-- 创建数据库（如果不存在）
CREATE DATABASE IF NOT EXISTS fishery 
    DEFAULT CHARACTER SET utf8mb4 
    COLLATE utf8mb4_unicode_ci;

USE fishery;

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
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '工单创建时间'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='人工工单表';

-- 索引：加速按鱼塘查询工单
CREATE INDEX IF NOT EXISTS idx_manual_tasks_pond_id ON manual_tasks(pond_id);

-- 索引：加速按状态筛选工单
CREATE INDEX IF NOT EXISTS idx_manual_tasks_status ON manual_tasks(status);

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
    update_time DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最后更新时间'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='设备映射表';

-- 索引：加速按鱼塘查询设备列表
CREATE INDEX IF NOT EXISTS idx_device_mapping_pond_id ON device_mapping(pond_id);

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
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最后更新时间'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='设备配置表';

-- 索引：加速按鱼塘查询设备列表
CREATE INDEX IF NOT EXISTS idx_control_devices_pond_id ON control_devices(pond_id);

-- 索引：加速按设备类型筛选
CREATE INDEX IF NOT EXISTS idx_control_devices_type ON control_devices(device_type);

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