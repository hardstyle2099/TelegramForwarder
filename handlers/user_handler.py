from enums.enums import UserForwardMode
import re
import logging
import asyncio
import time
from utils.common import check_keywords, get_sender_info

logger = logging.getLogger(__name__)

# 已处理的媒体组缓存：存储已处理过的 (source_chat_id, grouped_id) 对
# 防止同一个媒体组被重复转发
_processed_media_groups = set()  # {(chat_id, grouped_id), ...}
_processed_groups_lock = asyncio.Lock()


async def _mark_group_as_processed(chat_id, grouped_id):
    """标记一个媒体组为已处理"""
    async with _processed_groups_lock:
        _processed_media_groups.add((chat_id, grouped_id))


async def _is_group_processed(chat_id, grouped_id):
    """检查一个媒体组是否已处理过"""
    async with _processed_groups_lock:
        return (chat_id, grouped_id) in _processed_media_groups


async def process_forward_rule(client, event, chat_id, rule):
    """处理转发规则（用户模式）"""
    if not rule.enable_rule:
        logger.info(f'规则 ID: {rule.id} 已禁用，跳过处理')
        return

    # 媒体组去重：基于源聊天 ID + grouped_id，防止同一媒体组被重复处理
    if event.message.grouped_id:
        logger.info(f'[DEBUG] 检测到媒体组 - chat_id={event.chat_id}, grouped_id={event.message.grouped_id}, msg_id={event.message.id}')
        
        # 检查是否已处理过这个媒体组
        if await _is_group_processed(event.chat_id, event.message.grouped_id):
            logger.info(f'[DEBUG] 跳过已处理的媒体组 (grouped_id={event.message.grouped_id})')
            return
        
        # 标记为已处理
        await _mark_group_as_processed(event.chat_id, event.message.grouped_id)
        logger.info(f'[DEBUG] 处理新的媒体组 (grouped_id={event.message.grouped_id})')
        
        # 等待同组其他消息全部到达
        await asyncio.sleep(2)
    else:
        logger.info(f'[DEBUG] 单条消息 - msg_id={event.message.id}')

    message_text = event.message.text or ''
    check_message_text = message_text
    logger.info(f'处理规则 ID: {rule.id}')
    logger.info(f'消息内容: {message_text}')
    logger.info(f'规则模式: {rule.forward_mode.value}')

    if rule.is_filter_user_info:
        sender_info = await get_sender_info(event, rule.id)
        if sender_info:
            check_message_text = f"{sender_info}:\n{message_text}"
            logger.info(f'附带用户信息后的消息: {check_message_text}')
        else:
            logger.warning(f"规则 ID: {rule.id} - 无法获取发送者信息")

    should_forward = await check_keywords(rule, check_message_text)
    logger.info(f'最终决定: {"转发" if should_forward else "不转发"}')

    if should_forward:
        target_chat = rule.target_chat
        target_chat_id = int(target_chat.telegram_chat_id)
        try:
            # 读取用户模式发送方式，兼容旧数据库（没有该字段时默认 COPY - 隐藏来源）
            copy_mode = getattr(rule, 'user_copy_mode', UserForwardMode.COPY)

            if copy_mode == UserForwardMode.COPY:
                await _copy_send(client, event, target_chat_id, target_chat)
            else:
                await _native_forward(client, event, target_chat_id, target_chat)

        except Exception as e:
            logger.error(f'转发消息时出错: {str(e)}')
            logger.exception(e)


async def _native_forward(client, event, target_chat_id, target_chat):
    """原生转发模式：速度最快，但会显示 Forwarded from 来源头"""
    if event.message.grouped_id:
        await asyncio.sleep(1)
        messages = []
        async for message in client.iter_messages(
            event.chat_id,
            limit=20,
            min_id=event.message.id - 10,
            max_id=event.message.id + 10
        ):
            if message.grouped_id == event.message.grouped_id:
                messages.append(message.id)
                logger.info(f'找到媒体组消息: ID={message.id}')
        messages.sort()
        await client.forward_messages(target_chat_id, messages, event.chat_id)
        logger.info(f'[用户-转发] 已转发 {len(messages)} 条媒体组消息到: {target_chat.name} ({target_chat_id})')
    else:
        await client.forward_messages(target_chat_id, event.message.id, event.chat_id)
        logger.info(f'[用户-转发] 消息已转发到: {target_chat.name} ({target_chat_id})')


async def _copy_send(client, event, target_chat_id, target_chat):
    """
    复制发送模式：使用 send_file 隐藏"转发自"标记
    一次性发送整个媒体组，保持原始分组格式
    """
    message = event.message

    if message.grouped_id:
        # 媒体组：收集同组所有消息，一次性发送
        await asyncio.sleep(1)
        group_messages = []
        async for msg in client.iter_messages(
            event.chat_id,
            limit=20,
            min_id=message.id - 10,
            max_id=message.id + 10
        ):
            if msg.grouped_id == message.grouped_id:
                group_messages.append(msg)
                logger.info(f'找到媒体组消息: ID={msg.id}')
        
        # 按 ID 排序保持原顺序
        group_messages.sort(key=lambda m: m.id)
        
        if group_messages:
            # 收集媒体对象（直接使用 media 对象，不下载）
            media_list = [msg.media for msg in group_messages if msg.media]
            # 使用第一条消息的文本作为 caption
            caption = group_messages[0].text if group_messages else ''
            
            if media_list:
                # 一次性发送所有媒体，Telegram 会自动分组，隐藏"转发自"标记
                await client.send_file(target_chat_id, media_list, caption=caption)
                logger.info(f'[用户-复制] 已发送 {len(media_list)} 条媒体组消息到: {target_chat.name} ({target_chat_id}) - 隐藏转发标记')
            elif caption:
                await client.send_message(target_chat_id, caption)
                logger.info(f'[用户-复制] 已发送纯文本到: {target_chat.name} ({target_chat_id})')
    else:
        # 单条消息
        if message.media:
            await client.send_file(target_chat_id, message.media, caption=message.text or '')
            logger.info(f'[用户-复制] 单条媒体消息已发送到: {target_chat.name} ({target_chat_id}) - 隐藏转发标记')
        else:
            await client.send_message(target_chat_id, message.text or '')
            logger.info(f'[用户-复制] 单条文本消息已发送到: {target_chat.name} ({target_chat_id})')
