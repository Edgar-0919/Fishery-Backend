"""统一配置管理：传感器范围、告警阈值、数据库连接等，从 .env 读取"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # OneNET 回调校验模式：
    #   token     —— 校验 X-OneNET-Token / Authorization Bearer / ?token=（默认）
    #   signature —— 仅校验 MD5 签名（?msg=&nonce=&signature=）
    #   any       —— 签名或令牌任一通过即可
    #   none      —— 关闭校验，仅限本地联调
    ONENET_TOKEN: str = "fish"
    ONENET_VERIFY_MODE: str = "token"

    SENSOR_RANGES: dict = {
        'temperature': (-5, 45),
        'ph_value': (0, 14),
        'dissolved_oxygen': (0, 20),
        'ammonia_nitrogen': (0, 10),
        'nitrite': (0, 5),
        'ts_value': (0, 1000),
        'water_level': (0, 5),
    }

    # 告警阈值（detector 使用）：越界触发 critical/warning
    ALERT_THRESHOLDS: dict = {
        'temperature': {'min': 15.0, 'max': 32.0, 'warning_max': 28.0},
        'dissolved_oxygen': {'min': 3.0, 'max': 20.0, 'warning_min': 5.0},
        'ph_value': {'min': 6.5, 'max': 8.5},
        'ammonia_nitrogen': {'min': 0.0, 'max': 0.5, 'warning_max': 0.2},
        'nitrite': {'min': 0.0, 'max': 0.3, 'warning_max': 0.1},
        'ts_value': {'min': 0.0, 'max': 500.0},
        'water_level': {'min': 1.0, 'max': 3.0},
    }

    # 第一道防线：紧急阈值。命中即绕过 LLM 与 RAG 直接执行
    EMERGENCY_THRESHOLDS: dict = {
        'dissolved_oxygen_min': 1.0,
        'temperature_max': 35.0,
        'ph_value_min': 6.0,
        'ph_value_max': 9.5,
        'ammonia_nitrogen_max': 1.0,
        'water_level_min': 0.5,
    }

    # 执行前安全校验阈值：拦截会危及鱼类的危险操作
    SAFETY_THRESHOLDS: dict = {
        'dissolved_oxygen_min': 3.0,
        'temperature_max': 30.0,
        'ph_value_min': 6.5,
        'ph_value_max': 8.5,
    }

    # 效果跟踪阈值：指令执行后判断指标是否改善
    EFFECT_THRESHOLDS: dict = {
        'dissolved_oxygen_target': 5.0,   # 增氧目标值
        'dissolved_oxygen_rise': 1.0,     # 至少回升幅度
        'ammonia_nitrogen_drop': 0.1,     # 氨氮至少下降幅度
        'temperature_high': 28.0,         # 高于该温度才检查降温效果
    }

    # LLM 不可用时的规则兜底动作：按「指标 + 越界方向」查表
    # 阈值判断已由 detector 完成，这里只决定该指标异常时的处置动作
    FALLBACK_ACTIONS: dict = {
        'dissolved_oxygen': {
            'low': {
                'action': 'start_aerator',
                'params': {'duration': 60, 'power': 'high'},
                'reason': '溶解氧低于安全阈值，立即开启增氧机',
                'follow_up': '开启增氧机后持续监测，30 分钟后复测溶解氧',
            },
            'high': {
                'action': 'no_action',
                'params': {},
                'reason': '溶解氧过饱和，暂停增氧',
                'follow_up': '观察鱼群是否有气泡病症状',
            },
        },
        'ph_value': {
            'low': {
                'action': 'alert_human',
                'params': {'priority': 'high'},
                'reason': 'pH 值偏低，需人工检查并调节水质',
                'follow_up': '建议换水 20% 并投加适量生石灰调节 pH',
            },
            'high': {
                'action': 'alert_human',
                'params': {'priority': 'high'},
                'reason': 'pH 值偏高，需人工检查并调节水质',
                'follow_up': '建议换水 20% 并投加适量有机酸调节 pH',
            },
        },
        'ammonia_nitrogen': {
            'high': {
                'action': 'start_pump',
                'params': {'duration': 30},
                'reason': '氨氮浓度过高，开启水泵换水降低浓度',
                'follow_up': '换水后复测氨氮，如仍超标需减少投喂量',
            },
        },
        'nitrite': {
            'high': {
                'action': 'alert_human',
                'params': {'priority': 'medium'},
                'reason': '亚硝酸盐超标，需人工干预',
                'follow_up': '建议增开增氧机并投加硝化细菌制剂',
            },
        },
        'temperature': {
            'high': {
                'action': 'start_pump',
                'params': {'duration': 60},
                'reason': '水温过高会导致溶解氧下降，开启水泵换水降温',
                'follow_up': '持续监测水温变化，必要时增加换水频率',
            },
            'low': {
                'action': 'alert_human',
                'params': {'priority': 'medium'},
                'reason': '水温过低，鱼类摄食与生长受影响',
                'follow_up': '建议降低水位并覆盖保温膜',
            },
        },
        'ts_value': {
            'high': {
                'action': 'alert_human',
                'params': {'priority': 'low'},
                'reason': '浊度过高，影响光合作用与溶氧',
                'follow_up': '检查是否暴雨冲刷或藻类死亡，必要时换水',
            },
        },
        'water_level': {
            'low': {
                'action': 'alert_human',
                'params': {'priority': 'high'},
                'reason': '水位过低，存在缺氧与水温剧烈波动风险',
                'follow_up': '立即补水并检查是否有渗漏',
            },
            'high': {
                'action': 'alert_human',
                'params': {'priority': 'medium'},
                'reason': '水位过高，存在溢塘风险',
                'follow_up': '检查排水口是否堵塞，必要时开启排水',
            },
        },
    }

    # 趋势预警：仅当最近 N 个点整体变化幅度超过阈值才告警，避免自然波动误报
    TREND_WINDOW_POINTS: int = 5
    TREND_MIN_CHANGE_RATIO: float = 0.03
    TREND_ALERT_REPEAT_WINDOW_SECONDS: int = 600

    # 告警恢复：连续 N 个采样点回到正常范围才关闭活跃告警，避免临界值抖动反复开关
    RECOVERY_CONSECUTIVE_POINTS: int = 3
    # 兜底扫描间隔（秒）：处理进程重启丢失恢复计数、以及历史遗留的活跃告警
    RECOVERY_SCAN_INTERVAL_SECONDS: int = 300
    # 兜底扫描只认这个时间窗内的最新数据；超过则视为设备离线，不做恢复判断
    RECOVERY_DATA_MAX_AGE_SECONDS: int = 300

    MYSQL_HOST: str = "localhost"
    MYSQL_PORT: int = 3306
    MYSQL_USER: str = "root"
    MYSQL_PASSWORD: str = "root123"
    MYSQL_DATABASE: str = "fishery"

    ESCALATION_MINUTES: int = 60
    SCAN_INTERVAL_SECONDS: int = 30

    COOLDOWN_MINUTES: int = 5
    EFFECT_CHECK_MINUTES: int = 15
    HIGH_RISK_ACTIONS: list = ["stop_aerator", "stop_pump"]

    # 同一告警的重复写入抑制窗口（秒）：窗口内不重复写 alerts 表，
    # 但决策链路照常触发，避免持续异常把告警表刷爆
    ALERT_REPEAT_WINDOW_SECONDS: int = 60

    # LLM 配置的唯一来源；app/agent/config.py 只做兼容转发，不再自行解析 .env
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = ""
    LLM_MODEL: str = ""

    # 鉴权配置
    # 生产部署必须通过 .env 覆盖 JWT_SECRET，否则 token 可被伪造
    # HS256 要求密钥不少于 32 字节，生产环境必须覆盖
    JWT_SECRET: str = "dev-only-secret-change-me-in-production-env"
    JWT_EXPIRE_MINUTES: int = 720
    DEFAULT_ADMIN_USERNAME: str = "admin"
    DEFAULT_ADMIN_PASSWORD: str = "admin123"

    # 服务监听地址
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000

    # 允许的跨域来源，逗号分隔；生产环境应填具体域名
    CORS_ORIGINS: str = "*"

    # 日志级别：DEBUG / INFO / WARNING / ERROR
    LOG_LEVEL: str = "INFO"
    # 日志目录，留空则使用 Backend/logs
    LOG_DIR: str = ""

    # LLM 调用超时（秒）：问答与决策统一读取，避免一处 60s 一处 30s
    LLM_TIMEOUT: int = 60

    # RAG 检索：原先在 agent/core.py 模块级读取，
    # 运行时修改环境变量无效；统一收拢到 Settings
    RAG_ENABLED: bool = True
    RAG_TOP_K: int = 3
    RAG_SCORE_THRESHOLD: float = 0.5

    # ===== Agent =====
    # 决策链路唯一：ReAct 循环 + FALLBACK_ACTIONS 规则兜底，不存在可切换模式
    AGENT_MAX_STEPS: int = 6
    AGENT_MAX_ACTIONS: int = 2
    AGENT_TIMEOUT_SECONDS: int = 45
    AGENT_MAX_TOKENS: int = 4000
    AGENT_MIN_REQUEST_INTERVAL: float = 1.0
    AGENT_MAX_WORKERS: int = 2
    AGENT_HISTORY_TURNS: int = 6

    # Agent 健康度：窗口内 fallback 占比超过阈值即视为 LLM 侧整体退化。
    # 此时设备仍由 FALLBACK_ACTIONS 规则接管、不会停摆，所以必须显式暴露。
    AGENT_FALLBACK_WINDOW_MINUTES: int = 30
    AGENT_FALLBACK_MIN_SAMPLES: int = 10
    AGENT_FALLBACK_RATIO_THRESHOLD: float = 0.5
    AGENT_HEALTH_SCAN_SECONDS: int = 300

    # 上下文预算（字符数）：LLM 输入不再只按「消息条数」控制
    # 中文约 1 字符 ≈ 1 token，按 GLM 上下文窗口留足余量
    AGENT_HISTORY_MAX_CHARS: int = 6000
    AGENT_RAG_MAX_CHARS: int = 1500
    AGENT_DECISION_CONTEXT_MAX_CHARS: int = 4000
    AGENT_CONTEXT_HISTORY_LIMIT: int = 60

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()