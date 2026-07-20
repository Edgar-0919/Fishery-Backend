"""依赖注入模块：管理全局共享的 DataProcessor 单例实例"""

from app.services.data_processor import DataProcessor

_data_processor: DataProcessor | None = None


def get_data_processor() -> DataProcessor:
    global _data_processor
    if _data_processor is None:
        _data_processor = DataProcessor()
    return _data_processor


def set_data_processor(processor: DataProcessor):
    global _data_processor
    _data_processor = processor