"""统一配置管理：传感器范围、告警阈值、数据库连接等，从 .env 读取"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    ONENET_TOKEN: str = "fish"

    SENSOR_RANGES: dict = {
        'temperature': (-5, 45),
        'ph_value': (0, 14),
        'dissolved_oxygen': (0, 20),
        'ammonia_nitrogen': (0, 10),
        'nitrite': (0, 5),
        'ts_value': (0, 1000),
        'water_level': (0, 5),
    }

    ALERT_THRESHOLDS: dict = {
        'temperature': {'min': 15.0, 'max': 32.0, 'warning_max': 28.0},
        'dissolved_oxygen': {'min': 3.0, 'max': 20.0, 'warning_min': 5.0},
        'ph_value': {'min': 6.5, 'max': 8.5},
        'ammonia_nitrogen': {'min': 0.0, 'max': 0.5, 'warning_max': 0.2},
        'nitrite': {'min': 0.0, 'max': 0.3, 'warning_max': 0.1},
        'ts_value': {'min': 0.0, 'max': 500.0},
        'water_level': {'min': 1.0, 'max': 3.0},
    }

    TDENGINE_URL: str = "http://localhost:6041/rest/sql"
    TDENGINE_AUTH: str = "Basic cm9vdDp0YW9zZGF0YQ=="

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

    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = ""
    LLM_MODEL: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()