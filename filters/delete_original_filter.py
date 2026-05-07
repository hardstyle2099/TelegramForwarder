import logging
from filters.base_filter import BaseFilter
from utils.common import get_main_module

logger = logging.getLogger(__name__)

class DeleteOriginalFilter(BaseFilter):
    """
    删除原始消息过滤器，处理转发后是否要删除原始消息
    同时自动删除源频道的消息以隐藏转发标记
    """
    
    async def _process(self, context):
        """
        处理是否删除原始消息
        
        Args:
            context: 消息上下文
            
        Returns:
            bool: 是否继续处理
        """
        rule = context.rule
        event = context.event
        
        # 自动删除源频道消息以隐藏转发标记
        # 即使 is_delete_original 为 False，也要删除源频道的消息
        await self._delete_source_messages(event)
        
        # 如果需要删除原始消息（备用逻辑）
        if rule.is_delete_original:
            try:
                # 获取 main.py 中的用户客户端
                main = await get_main_module()
                user_client = main.user_client  # 获取用户客户端
                
                # 媒体组消息
                if event.message.grouped_id:
                    # 使用用户客户端获取并删除媒体组消息
                    async for message in user_client.iter_messages(
                            event.chat_id,
                            min_id=event.message.id - 10,
                            max_id=event.message.id + 10,
                            reverse=True
                    ):
                        if message.grouped_id == event.message.grouped_id:
                            await message.delete()
                            logger.info(f'已删除媒体组消息 ID: {message.id}')
                else:
                    # 单条消息的删除逻辑
                    message = await user_client.get_messages(event.chat_id, ids=event.message.id)
                    await message.delete()
                    logger.info(f'已删除原始消息 ID: {event.message.id}')
                    
            except Exception as e:
                logger.error(f'删除原始消息时出错: {str(e)}')
                context.errors.append(f"删除原始消息错误: {str(e)}")
        
        return True  # 继续处理
    
    async def _delete_source_messages(self, event):
        """
        删除源频道的消息以隐藏转发标记
        """
        try:
            main = await get_main_module()
            user_client = main.user_client
            
            # 媒体组消息
            if event.message.grouped_id:
                deleted_count = 0
                async for message in user_client.iter_messages(
                        event.chat_id,
                        min_id=event.message.id - 10,
                        max_id=event.message.id + 10,
                        reverse=True
                ):
                    if message.grouped_id == event.message.grouped_id:
                        try:
                            await message.delete()
                            deleted_count += 1
                            logger.info(f'已隐藏源频道媒体组消息 ID: {message.id}')
                        except Exception as e:
                            logger.warning(f'删除媒体组消息 ID: {message.id} 失败: {str(e)}')
                
                if deleted_count > 0:
                    logger.info(f'成功隐藏源频道媒体组消息，共 {deleted_count} 条')
            else:
                # 单条消息
                try:
                    message = await user_client.get_messages(event.chat_id, ids=event.message.id)
                    await message.delete()
                    logger.info(f'已隐藏源频道原始消息 ID: {event.message.id}')
                except Exception as e:
                    logger.warning(f'删除源频道消息失败: {str(e)}')
                    
        except Exception as e:
            logger.error(f'隐藏源频道消息时出错: {str(e)}') 
