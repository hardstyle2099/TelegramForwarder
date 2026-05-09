import logging
import os
from filters.base_filter import BaseFilter
from enums.enums import PreviewMode
from telethon.errors import FloodWaitError

logger = logging.getLogger(__name__)

class SenderFilter(BaseFilter):
    """
    消息发送过滤器，用于发送处理后的消息
    """
    
    async def _process(self, context):
        """
        发送处理后的消息
        
        Args:
            context: 消息上下文
            
        Returns:
            bool: 是否继续处理
        """
        rule = context.rule
        client = context.client
        event = context.event
        
        if not context.should_forward:
            logger.info('消息不满足转发条件，跳过发送')
            return False
        
        if rule.enable_only_push:
            logger.info('只转发到推送配置，跳过发送')
            return True
            
        # 获取目标聊天信息
        target_chat = rule.target_chat
        target_chat_id = int(target_chat.telegram_chat_id)
        
        # 预先获取目标聊天实体
        try:
            entity = None
            try:
                # 直接使用ID
                entity = await client.get_entity(target_chat_id)
                logger.info(f'成功获取目标聊天实体: {target_chat.name} (ID: {target_chat_id})')
            except Exception as e1:
                try:
                    # 尝试添加-100前缀
                    if not str(target_chat_id).startswith('-100'):
                        super_group_id = int(f'-100{abs(target_chat_id)}')
                        entity = await client.get_entity(super_group_id)
                        target_chat_id = super_group_id  # 更新使用正确的ID
                        logger.info(f'使用私有群组ID格式成功获取实体: {target_chat.name} (ID: {target_chat_id})')
                except Exception as e2:
                    try:
                        # 尝试常规群组格式
                        if not str(target_chat_id).startswith('-'):
                            group_id = int(f'-{abs(target_chat_id)}')
                            entity = await client.get_entity(group_id)
                            target_chat_id = group_id  # 更新使用正确的ID
                            logger.info(f'使用常规群组ID格式成功获取实体: {target_chat.name} (ID: {target_chat_id})')
                    except Exception as e3:
                        logger.warning(f'无法获取目标聊天实体，尝试继续发送: {e1}, {e2}, {e3}')
        except Exception as e:
            logger.warning(f'获取目标聊天实体时出错: {str(e)}')
        
        # 设置消息格式
        parse_mode = rule.message_mode.value  # 使用枚举的值（字符串）
        logger.info(f'使用消息格式: {parse_mode}')
        
        try:
            # 处理媒体组消息
            if context.is_media_group or (context.media_group_messages and context.skipped_media):
                logger.info(f'准备发送媒体组消息')
                await self._send_media_group(context, target_chat_id, parse_mode)
            # 处理单条媒体消息
            elif context.media_files or context.skipped_media:
                logger.info(f'准备发送单条媒体消息')
                await self._send_single_media(context, target_chat_id, parse_mode)
            # 处理纯文本消息
            else:
                logger.info(f'准备发送纯文本消息')
                await self._send_text_message(context, target_chat_id, parse_mode)
                
            logger.info(f'消息已发送到: {target_chat.name} ({target_chat_id})')
            return True
        except FloodWaitError as e:
            wait_time = e.seconds
            logger.error(f'发送消息频率限制，需要等待 {wait_time} 秒')
            context.errors.append(f"发送消息频率限制，需要等待 {wait_time} 秒")
            return False
        except Exception as e:
            logger.error(f'发送消息时出错: {str(e)}')
            context.errors.append(f"发送消息错误: {str(e)}")
            return False
    
    async def _send_media_group(self, context, target_chat_id, parse_mode):
        """
        发送媒体组消息 - 使用 forward_messages 保持完整的媒体组结构
        
        重要：send_file 无法保持媒体组的 grouped_id 绑定，必须使用 forward_messages
        """
        client = context.client
        event = context.event
        # 初始化转发消息列表
        context.forwarded_messages = []
        
        try:
            # 收集媒体组的所有消息ID
            message_ids = []
            for message in context.media_group_messages:
                if message.media:
                    message_ids.append(message.id)
            
            if message_ids:
                # 按ID排序，保持原有顺序
                message_ids.sort()
                
                # 使用 forward_messages 转发，这样可以完整保留媒体组结构
                sent_messages = await client.forward_messages(
                    target_chat_id,
                    message_ids,
                    event.chat_id
                )
                
                # 保存发送的消息到上下文
                if isinstance(sent_messages, list):
                    context.forwarded_messages = sent_messages
                else:
                    context.forwarded_messages = [sent_messages]
                
                logger.info(f'媒体组消息已转发（保持媒体组结构），共 {len(context.forwarded_messages)} 条消息')
        except Exception as e:
            logger.error(f'发送媒体组消息时出错: {str(e)}')
            raise
    
    async def _send_single_media(self, context, target_chat_id, parse_mode):
        """
        发送单条媒体消息 - 使用 forward_messages 保持原始属性
        """
        client = context.client
        event = context.event
        
        logger.info(f'发送单条媒体消息')
        
        # 检查是否所有媒体都超限
        if context.skipped_media and not context.media_files:
            # 构建提示信息
            file_size = context.skipped_media[0][1]
            file_name = context.skipped_media[0][2]
            original_link = f"\n原始消息: https://t.me/c/{str(event.chat_id)[4:]}/{event.message.id}"
            
            text_to_send = context.message_text or ''
            text_to_send += f"\n\n⚠️ 媒体文件 {file_name} ({file_size}MB) 超过大小限制"
            text_to_send = context.sender_info + text_to_send + context.time_info
            
            text_to_send += original_link
                
            await client.send_message(
                target_chat_id,
                text_to_send,
                parse_mode=parse_mode,
                link_preview=True,
                buttons=context.buttons
            )
            logger.info(f'媒体文件超过大小限制，仅转发文本')
            return
        
        # 使用 forward_messages 转发单条消息
        try:
            await client.forward_messages(
                target_chat_id,
                event.message.id,
                event.chat_id
            )
            logger.info(f'单条媒体消息已转发')
        except Exception as e:
            logger.error(f'发送媒体消息时出错: {str(e)}')
            raise
    
    async def _send_text_message(self, context, target_chat_id, parse_mode):
        """发送纯文本消息"""
        rule = context.rule
        client = context.client
        
        if not context.message_text:
            logger.info('没有文本内容，不发送消息')
            return
            
        # 根据预览模式设置 link_preview
        link_preview = {
            PreviewMode.ON: True,
            PreviewMode.OFF: False,
            PreviewMode.FOLLOW: context.event.message.media is not None  # 跟随原消息
        }[rule.is_preview]
        
        # 组合消息文本
        message_text = context.sender_info + context.message_text + context.time_info + context.original_link
        
        await client.send_message(
            target_chat_id,
            str(message_text),
            parse_mode=parse_mode,
            link_preview=link_preview,
            buttons=context.buttons
        )
        logger.info(f'{"带预览的" if link_preview else "无预览的"}文本消息已发送') 
