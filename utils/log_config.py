import os
import logging
from pathlib import Path
from dotenv import load_dotenv

def setup_logging():
    """
    配置日志系统，将所有日志输出到标准输出，
    由Docker收集并管理日志
    """
    # 加载环境变量
    load_dotenv()
    
    # 创建根日志记录器
    root_logger = logging.getLogger()
    
    # 设置日志级别 - 默认使用INFO级别
    root_logger.setLevel(logging.INFO)
    
    # 创建一个处理器，用于将日志输出到控制台
    console_handler = logging.StreamHandler()
    
    # 创建格式化器
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    
    # 将格式化器添加到处理器
    console_handler.setFormatter(formatter)
    
    # 将处理器添加到根日志记录器
    root_logger.addHandler(console_handler)
    
    # 返回配置的日志记录器
    return root_logger 