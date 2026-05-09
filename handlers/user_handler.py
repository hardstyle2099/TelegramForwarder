from enums.enums import UserForwardMode
import re
import logging
import asyncio
import time
from utils.common import check_keywords, get_sender_info

logger = logging.getLogger(__name__)

# 已处理的媒体组缓存：{(rule_id, grouped_id): timestamp}
# 同一规则中，同一媒体组只处理一次，3秒内自动过期
_processed_groups = {}


def _is_first_message_in_group(rule_id, grouped_id):
    """检查是否是该媒体组在当前规则中的第一条消息（非异步，避免 Lock 问题）"""
    cache_key = (rule_id, grouped_id)
    current_time = time.time()
    
    # 清理过期的缓存（3秒）
    expired_keys = [k for k, v in _processed_groups.items() if current_time - v > 3]
    for k in expired_keys:
        del _processed_groups[k]
    
    # 检查是否已处理
    if cache_key in _processed_groups:
        logger.info(f'规则 ID: {rule_id} 跳过重复媒体组消息 (grouped_id={grouped_id})')
        return False
    
    # 标记为已处理
    _processed_groups[cache_key] = current_time
    logger.info(f'规则 ID: {rule_id} 处理媒体组消息 (grouped_id={grouped_id})')
    return True


async def process_forward_rule(client, event, chat_id, rule):
    """处理转发规则（用户模式）"""
    if not rule.enable_rule:
        logger.info(f'规则 ID: {rule.id} 已禁用，跳过处理')
        return

    # 媒体组去重：同一规则中，同一媒体组只处理一次
    if event.message.grouped_id:
        logger.info(f'[DEBUG] 检测到媒体组: grouped_id={event.message.grouped_id}, msg_id={event.message.id}')
        is_first = _is_first_message_in_group(rule.id, event.message.grouped_id)
        logger.info(f'[DEBUG] 去重结果: is_first={is_first}')
        if not is_first:
            logger.info(f'[DEBUG] 跳过处理此消息')
            return
        # 等待同组其他消息全部到达
        await asyncio.sleep(2)
    else:
        logger.info(f'[DEBUG] 单条消息, msg_id={event.message.id}')

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
    复制发送模式：使用 forward_messages 保持完整的媒体组结构
    然后删除源频道消息以隐藏"转发自"标记
    """
    message = event.message

    if message.grouped_id:
        # 媒体组：收集同组所有消息 ID，然后转发
        await asyncio.sleep(1)
        message_ids = []
        async for msg in client.iter_messages(
            event.chat_id,
            limit=20,
            min_id=message.id - 10,
            max_id=message.id + 10
        ):
            if msg.grouped_id == message.grouped_id:
                message_ids.append(msg.id)
                logger.info(f'找到媒体组消息: ID={msg.id}')
        
        message_ids.sort()
        
        if message_ids:
            # 使用 forward_messages 转发，保持媒体组结构
            await client.forward_messages(target_chat_id, message_ids, event.chat_id)
            logger.info(f'[用户-复制] 已转发 {len(message_ids)} 条媒体组消息到: {target_chat.name} ({target_chat_id})')
            
            # 删除源频道消息以隐藏"转发自"标记
            try:
                await client.delete_messages(event.chat_id, message_ids)
                logger.info(f'已删除源频道的 {len(message_ids)} 条消息（隐藏转发标记）')
            except Exception as e:
                logger.warning(f'删除源频道消息失败: {str(e)}')
    else:
        # 单条消息
        if message.media:
            await client.forward_messages(target_chat_id, message.id, event.chat_id)
            logger.info(f'[用户-复制] 单条媒体消息已转发到: {target_chat.name} ({target_chat_id})')
            
            # 删除源频道消息
            try:
                await client.delete_messages(event.chat_id, [message.id])
                logger.info(f'已删除源频道消息（隐藏转发标记）')
            except Exception as e:
                logger.warning(f'删除源频道消息失败: {str(e)}')
        else:
            await client.send_message(target_chat_id, message.text or '')
            logger.info(f'[用户-复制] 单条文本消息已发送到: {target_chat.name} ({target_chat_id})')
