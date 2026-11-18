-- 创建数据库（如果不存在）
CREATE DATABASE IF NOT EXISTS fishery 
    DEFAULT CHARACTER SET utf8mb4 
    COLLATE utf8mb4_unicode_ci;

USE fishery;

-- ----------------------------------------------------------------
-- 用户表
-- 用途：登录鉴权，密码以 pbkdf2_sha256 哈希存储，不保存明文
-- 初始化：表为空时后端启动会自动创建默认管理员（见 app/core/users.py）
--        默认账号 admin / admin123，首次登录后请立即修改
-- ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT '用户ID（自增主键）',
    username VARCHAR(50) NOT NULL UNIQUE COMMENT '登录名',
    password_hash VARCHAR(255) NOT NULL COMMENT '密码哈希（pbkdf2_sha256$迭代$盐$哈希）',
    display_name VARCHAR(50) COMMENT '显示名称',
    role VARCHAR(20) DEFAULT 'operator' COMMENT '角色：admin/operator',
    is_active TINYINT(1) DEFAULT 1 COMMENT '是否启用：0停用/1启用',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户表';

-- ----------------------------------------------------------------
-- 人工工单表
-- 用途：存储告警持续超过 1 小时后自动升级的人工处理任务
-- 数据来源：alert_tracker.py 检测到超时告警后自动创建
-- ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS manual_tasks (
    id BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT '工单ID（自增主键）',
    pond_id VARCHAR(50) NOT NULL COMMENT '鱼塘ID（关联告警所属鱼塘）',
    alert_type VARCHAR(50) NOT NULL COMMENT '告警类型（如 dissolved_oxygen_low）',
    alert_value DOUBLE COMMENT '触发告警时的异常值',
    threshold_value DOUBLE COMMENT '告警阈值',
    started_at DATETIME COMMENT '异常开始时间',
    escalated_at DATETIME COMMENT '升级为人工工单的时间',
    status ENUM('pending', 'processing', 'resolved') DEFAULT 'pending' COMMENT '工单状态：pending(待处理)/processing(处理中)/resolved(已解决)',
    handler VARCHAR(50) COMMENT '处理人',
    remark TEXT COMMENT '处理备注',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '工单创建时间',
    INDEX idx_manual_tasks_pond_id (pond_id),
    INDEX idx_manual_tasks_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='人工工单表';

-- ----------------------------------------------------------------
-- 设备映射表
-- 用途：存储设备与鱼塘的关联关系，支持通过设备ID快速定位所属鱼塘
-- 数据来源：系统初始化时预设，可通过管理后台维护
-- ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS device_mapping (
    id BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT '记录ID（自增主键）',
    device_id VARCHAR(50) NOT NULL UNIQUE COMMENT '设备ID（唯一标识，OneNET/模拟器设备编号）',
    pond_id VARCHAR(50) NOT NULL COMMENT '鱼塘ID（设备所属鱼塘）',
    pond_name VARCHAR(100) COMMENT '鱼塘名称（冗余字段，便于显示）',
    location VARCHAR(255) COMMENT '位置描述（如 养殖场A区）',
    update_time DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最后更新时间',
    INDEX idx_device_mapping_pond_id (pond_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='设备映射表';

-- ----------------------------------------------------------------
-- 设备配置表
-- 用途：存储可控设备（增氧机、水泵、投饵机）的配置信息和运行状态
-- 数据来源：系统初始化时预设，状态由 Agent/人工指令更新
-- ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS control_devices (
    id BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT '记录ID（自增主键）',
    pond_id VARCHAR(50) NOT NULL COMMENT '鱼塘ID（设备所属鱼塘）',
    device_type VARCHAR(50) NOT NULL COMMENT '设备类型：aerator(增氧机)/pump(水泵)/feeder(投饵机)',
    device_name VARCHAR(100) COMMENT '设备名称（如 增氧机1号）',
    device_identifier VARCHAR(100) COMMENT '硬件标识/MQTT topic（用于实际控制指令下发）',
    status ENUM('online', 'offline', 'running') DEFAULT 'offline' COMMENT '设备状态：online(在线)/offline(离线)/running(运行中)',
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最后更新时间',
    -- 设备标识是控制指令的路由键，必须唯一；唯一索引也让初始化脚本的
    -- INSERT IGNORE 真正具备幂等性，重复执行不会插入重复设备
    UNIQUE KEY uk_control_devices_identifier (device_identifier),
    INDEX idx_control_devices_pond_id (pond_id),
    INDEX idx_control_devices_type (device_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='设备配置表';

-- ----------------------------------------------------------------
-- 告警事件表
-- 用途：记录每次触发的告警明细
-- 迁移说明：原建在 TDengine 普通表，ts 为唯一主键，同一秒多个指标告警
--          会互相覆盖导致告警丢失；改存 MySQL 后无此问题，且支持标记已读
-- ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alerts (
    id BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT '告警ID（自增主键）',
    ts DATETIME(3) NOT NULL COMMENT '告警发生时间（毫秒精度）',
    pond_id VARCHAR(50) NOT NULL COMMENT '鱼塘ID',
    device_id VARCHAR(50) NOT NULL COMMENT '设备ID',
    alert_type VARCHAR(50) NOT NULL COMMENT '告警类型（如 dissolved_oxygen_low）',
    alert_value DOUBLE COMMENT '触发告警时的异常值',
    threshold_value DOUBLE COMMENT '告警阈值',
    message VARCHAR(500) COMMENT '告警描述',
    is_read TINYINT(1) DEFAULT 0 COMMENT '是否已读：0未读/1已读',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    INDEX idx_alerts_pond_ts (pond_id, ts),
    INDEX idx_alerts_dedup (pond_id, device_id, alert_type, ts),
    INDEX idx_alerts_read (is_read)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='告警事件表';

-- ----------------------------------------------------------------
-- 告警持续追踪表
-- 用途：记录异常持续时长，超过阈值后升级为人工工单
-- 迁移说明：原建在 TDengine 并用 DELETE+INSERT 模拟 UPDATE，并发下会丢记录；
--          改存 MySQL 后可用真正的 UPDATE，且 id 主键天然唯一
-- ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alert_duration (
    id BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT '记录ID（自增主键）',
    pond_id VARCHAR(50) NOT NULL COMMENT '鱼塘ID',
    device_id VARCHAR(50) NOT NULL COMMENT '设备ID',
    alert_type VARCHAR(50) NOT NULL COMMENT '告警类型',
    alert_value DOUBLE COMMENT '最近一次异常值',
    threshold_value DOUBLE COMMENT '告警阈值',
    started_at DATETIME NOT NULL COMMENT '异常开始时间',
    last_checked_at DATETIME COMMENT '最后一次扫描时间',
    duration_minutes INT DEFAULT 0 COMMENT '已持续分钟数',
    status VARCHAR(20) DEFAULT 'active' COMMENT '状态：active/escalated/resolved',
    agent_decision TEXT COMMENT 'Agent 决策分析',
    agent_action VARCHAR(100) COMMENT 'Agent 下发的动作',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    INDEX idx_alert_duration_active (status, started_at),
    INDEX idx_alert_duration_lookup (pond_id, device_id, alert_type, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='告警持续追踪表';

-- ----------------------------------------------------------------
-- 设备指令记录表
-- 用途：记录 Agent/人工下发的所有设备控制指令及执行结果
-- 迁移说明：原建在 TDengine 且用 ts 作主键，同秒多条指令会互相覆盖；
--          改存 MySQL 后支持状态流转更新与效果检查扫描
-- ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS device_commands (
    id BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT '指令ID（自增主键）',
    ts DATETIME(3) NOT NULL COMMENT '指令下发时间（毫秒精度）',
    pond_id VARCHAR(50) NOT NULL COMMENT '鱼塘ID',
    device_id VARCHAR(50) NOT NULL COMMENT '设备ID',
    command_type VARCHAR(50) NOT NULL COMMENT '指令类型（如 start_aerator）',
    trigger_source VARCHAR(50) DEFAULT 'agent' COMMENT '触发来源：agent/manual/scheduled',
    trigger_alert_id VARCHAR(100) COMMENT '触发该指令的告警类型',
    params TEXT COMMENT '指令参数 JSON',
    status VARCHAR(20) DEFAULT 'pending' COMMENT '状态：pending_approval/executed/failed',
    executed_at DATETIME COMMENT '执行时间',
    result VARCHAR(500) COMMENT '执行结果描述',
    effect_checked TINYINT(1) DEFAULT 0 COMMENT '是否已完成效果检查：0未检查/1已检查',
    sensor_snapshot TEXT COMMENT '执行前的传感器快照 JSON，用于效果比对',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    INDEX idx_device_commands_pond_ts (pond_id, ts),
    INDEX idx_device_commands_effect (effect_checked, status, executed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='设备指令记录表';

-- ----------------------------------------------------------------
-- Agent 决策审计表
-- 用途：记录每次 Agent 运行的原始提议、RAG 召回、最终动作与降级原因
-- 说明：只做审计，长时程业务状态仍以 alert_duration / device_commands 为准
-- ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_runs (
    id BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT '记录ID',
    trace_id VARCHAR(64) NOT NULL COMMENT '决策追踪ID',
    pond_id VARCHAR(50) COMMENT '鱼塘ID',
    device_id VARCHAR(50) COMMENT '设备ID',
    alert_type VARCHAR(50) COMMENT '告警类型',
    mode VARCHAR(20) DEFAULT 'react' COMMENT '运行模式（固定 react，历史数据可能含 legacy/shadow）',
    started_at DATETIME(3) COMMENT '开始时间',
    finished_at DATETIME(3) COMMENT '结束时间',
    steps INT DEFAULT 0 COMMENT 'LLM 循环轮次',
    prompt_tokens INT DEFAULT 0 COMMENT '输入 token',
    completion_tokens INT DEFAULT 0 COMMENT '输出 token',
    latency_ms INT DEFAULT 0 COMMENT '总耗时（毫秒）',
    outcome VARCHAR(30) DEFAULT 'pending' COMMENT '结果：executed/blocked/fallback/error/dry_run（skipped 为历史值）',
    fallback_reason VARCHAR(255) COMMENT '降级原因',
    proposals JSON COMMENT 'Guard 前的原始动作提议',
    rag_queries JSON COMMENT '检索 query 与召回来源',
    final_actions JSON COMMENT '实际送执行器的动作',
    error TEXT COMMENT '异常信息',
    UNIQUE KEY uk_agent_runs_trace (trace_id),
    INDEX idx_agent_runs_time (started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Agent 决策审计表';

-- ----------------------------------------------------------------
-- 问答会话表
-- 用途：为 /api/agent/chat 提供跨请求、跨进程重启的多轮上下文
-- ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chat_sessions (
    id BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT '记录ID',
    session_id VARCHAR(64) NOT NULL COMMENT '会话ID',
    user_id VARCHAR(50) COMMENT '所属用户',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    UNIQUE KEY uk_chat_sessions_session (session_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='问答会话表';

CREATE TABLE IF NOT EXISTS chat_messages (
    id BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT '消息ID',
    session_id VARCHAR(64) NOT NULL COMMENT '会话ID',
    role VARCHAR(20) NOT NULL COMMENT '角色：user/assistant',
    content TEXT COMMENT '消息内容',
    sources JSON COMMENT 'RAG 参考来源',
    created_at DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3) COMMENT '创建时间',
    INDEX idx_chat_messages_session (session_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='问答消息表';

-- ----------------------------------------------------------------
-- 传感器时序数据表（原 TDengine 超级表，已合并到 MySQL）
-- 迁移说明：原用 TDengine 超级表 + 子表按设备拆分，避免 ts 主键同秒覆盖；
--          MySQL 用 (device_id, ts) 联合主键达到同样效果。
--          数据量增大后可按月 RANGE 分区；当前规模（5 秒上报）单表足够。
-- ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sensor_data (
    ts DATETIME(3) NOT NULL COMMENT '采集时间（毫秒精度）',
    pond_id VARCHAR(50) NOT NULL COMMENT '鱼塘ID',
    device_id VARCHAR(50) NOT NULL COMMENT '采集设备ID',
    product_id VARCHAR(50) COMMENT '产品ID',
    temperature FLOAT COMMENT '水温(℃)',
    ph_value FLOAT COMMENT 'pH',
    dissolved_oxygen FLOAT COMMENT '溶解氧(mg/L)',
    ammonia_nitrogen FLOAT COMMENT '氨氮(mg/L)',
    nitrite FLOAT COMMENT '亚硝酸盐(mg/L)',
    ts_value FLOAT COMMENT '浊度(NTU)',
    water_level FLOAT COMMENT '水位(m)',
    PRIMARY KEY (device_id, ts),
    INDEX idx_sensor_data_pond_ts (pond_id, ts),
    INDEX idx_sensor_data_ts (ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='传感器时序数据表';

-- ----------------------------------------------------------------
-- 设备上下线时序表（原 TDengine 超级表 device_status）
-- ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS device_status (
    ts DATETIME(3) NOT NULL COMMENT '状态变更时间',
    device_id VARCHAR(50) NOT NULL COMMENT '设备ID',
    status TINYINT COMMENT '状态：1上线/0下线',
    login_type TINYINT COMMENT '登录类型',
    PRIMARY KEY (device_id, ts),
    INDEX idx_device_status_ts (ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='设备上下线时序表';

-- ----------------------------------------------------------------
-- 初始化测试数据
-- ----------------------------------------------------------------

-- 设备映射测试数据
INSERT IGNORE INTO device_mapping (device_id, pond_id, pond_name, location) VALUES
('sim_dev_001', 'pond1', '一号鱼塘', '养殖场A区'),
('sim_dev_002', 'pond2', '二号鱼塘', '养殖场A区'),
('sim_dev_003', 'pond3', '三号鱼塘', '养殖场B区');

-- 可控设备测试数据
INSERT IGNORE INTO control_devices (pond_id, device_type, device_name, device_identifier, status) VALUES
('pond1', 'aerator', '增氧机1号', 'aerator_pond1', 'online'),
('pond1', 'pump', '水泵1号', 'pump_pond1', 'online'),
('pond2', 'aerator', '增氧机2号', 'aerator_pond2', 'online'),
('pond2', 'pump', '水泵2号', 'pump_pond2', 'online');