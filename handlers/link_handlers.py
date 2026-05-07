# handlers/link_handlers.py

import re
import io
import asyncio
import time
import logging
import numpy as np
from telethon.tl.types import DocumentAttributeVideo
from utils.common import get_main_module

logger = logging.getLogger(__name__)

# 进度更新时间控制
_last_progress_update = {}


async def handle_message_link(bot_client, event):
    """
    处理 Telegram 消息链接 - 获取受限频道视频
    
    特性：
    - 显示下载/上传进度
    - 支持流媒体播放（无需下载即可观看）
    - 视频发送到当前聊天（不是收藏）
    """
    if not event.message.text:
        return

    # 解析消息链接
    match = re.match(r'https?://t\.me/(?:c/(\d+)|([^/]+))/(\d+)', event.message.text.strip())
    if not match:
        return

    try:
        chat_id = None
        message_id = int(match.group(3))

        # 获取用户客户端
        main = await get_main_module()
        user_client = main.user_client

        if match.group(1):  # 私有频道格式
            chat_id = int('-100' + match.group(1))
        else:  # 公开频道格式
            chat_name = match.group(2)
            try:
                entity = await user_client.get_entity(chat_name)
                chat_id = entity.id
            except Exception as e:
                logger.error(f'获取频道信息失败: {str(e)}')
                await event.reply('无法访问该频道，请确保已加入该频道。')
                return

        # 获取原始消息
        message = await user_client.get_messages(chat_id, ids=message_id)
        if not message:
            await event.reply('无法获取该消息，可能消息已被删除或无权限。')
            return

        if not message.media:
            await event.reply('该消息不包含媒体文件。')
            return

        # 获取文件大小并显示状态
        file_size = _get_file_size(message)
        status_msg = await event.reply(f'开始处理...\n文件大小: {_format_size(file_size)}')

        # 目标聊天（发送到当前聊天，不是收藏）
        target_chat = event.chat_id

        # 检查是否是媒体组消息
        # 只下载单个视频（不管是否属于媒体组）
        await _handle_single_message(user_client, bot_client, message, target_chat, status_msg)

        # 删除状态消息
        await status_msg.delete()
        logger.info(f'[LinkHandler] 成功获取受限消息: chat_id={chat_id}, message_id={message_id}')

    except Exception as e:
        logger.error(f'处理消息链接时出错: {str(e)}')
        await event.reply(f'处理消息时出错: {str(e)}')


async def _handle_media_group(user_client, bot_client, chat_id, message, target_chat, status_msg):
    """处理媒体组"""
    try:
        # 收集媒体组的所有消息
        media_group_messages = []
        caption = None

        async for grouped_message in user_client.iter_messages(
            chat_id,
            limit=20,
            min_id=message.id - 10,
            max_id=message.id + 10
        ):
            if grouped_message.grouped_id == message.grouped_id:
                media_group_messages.append(grouped_message)
                if not caption:
                    caption = grouped_message.text

        if not media_group_messages:
            return

        media_group_messages.sort(key=lambda m: m.id)
        total_files = len(media_group_messages)

        # 下载所有媒体到内存
        media_files = []
        for i, msg in enumerate(media_group_messages):
            if msg.media:
                await status_msg.edit(f'下载中 ({i+1}/{total_files})...')
                buffer = io.BytesIO()
                await user_client.download_media(
                    msg, 
                    buffer,
                    progress_callback=lambda current, total: asyncio.create_task(
                        _update_progress(status_msg, current, total, f'下载 {i+1}/{total_files}')
                    )
                )
                buffer.seek(0)
                buffer.name = _get_filename(msg)
                media_files.append(buffer)

        if media_files:
            await status_msg.edit('上传中...')
            # 用 bot_client 发送，支持流媒体
            await bot_client.send_file(
                target_chat,
                media_files,
                caption=caption or '',
                supports_streaming=True
            )
            logger.info(f'[LinkHandler] 已发送媒体组，共 {len(media_files)} 个文件')

    except Exception as e:
        logger.error(f'处理媒体组消息时出错: {str(e)}')
        raise


async def _handle_single_message(user_client, bot_client, message, target_chat, status_msg):
    """处理单条消息"""
    try:
        import cv2
        import numpy as np
        
        # 下载到内存
        buffer = io.BytesIO()
        await user_client.download_media(
            message, 
            buffer,
            progress_callback=lambda current, total: asyncio.create_task(
                _update_progress(status_msg, current, total, '下载中')
            )
        )
        buffer.seek(0)
        buffer.name = _get_filename(message)

        # 获取视频属性
        video_attrs = _get_video_attributes(message)
        
        # 从视频中随机抽取一帧作为缩略图
        thumb = None
        try:
            thumb = await _extract_random_frame(buffer)
        except Exception as e:
            logger.warning(f'提取视频缩略图失败: {str(e)}')
            pass

        # 用 bot_client 上传，支持流媒体
        await status_msg.edit('上传中... 0%')
        await bot_client.send_file(
            target_chat,
            buffer,
            caption=message.text or '',
            supports_streaming=True,
            attributes=video_attrs,
            thumb=thumb,
            progress_callback=lambda current, total: asyncio.create_task(
                _update_progress(status_msg, current, total, '上传中')
            )
        )
        logger.info('[LinkHandler] 已发送单条媒体消息')

    except Exception as e:
        logger.error(f'处理单条消息时出错: {str(e)}')
        raise


async def _update_progress(status_msg, current, total, prefix):
    """更新进度显示（带节流，避免频繁编辑）"""
    msg_id = status_msg.id
    now = time.time()
    
    # 每 2 秒更新一次，或者完成时更新
    if msg_id in _last_progress_update:
        if now - _last_progress_update[msg_id] < 2 and current < total:
            return
    
    _last_progress_update[msg_id] = now
    
    if total > 0:
        percent = current / total * 100
        progress_bar = _create_progress_bar(percent)
        try:
            await status_msg.edit(
                f'{prefix}...\n'
                f'{progress_bar} {percent:.1f}%\n'
                f'{_format_size(current)} / {_format_size(total)}'
            )
        except Exception:
            pass


def _create_progress_bar(percent, length=20):
    """创建进度条"""
    filled = int(length * percent / 100)
    bar = '█' * filled + '░' * (length - filled)
    return f'[{bar}]'


def _format_size(size):
    """格式化文件大小"""
    if size < 1024:
        return f'{size} B'
    elif size < 1024 * 1024:
        return f'{size / 1024:.1f} KB'
    elif size < 1024 * 1024 * 1024:
        return f'{size / 1024 / 1024:.1f} MB'
    else:
        return f'{size / 1024 / 1024 / 1024:.2f} GB'


def _get_file_size(message):
    """获取文件大小"""
    if message.document:
        return message.document.size
    elif message.video:
        return message.video.size if hasattr(message.video, 'size') else 0
    return 0


def _get_filename(message):
    """获取文件名"""
    if message.document:
        for attr in message.document.attributes:
            if hasattr(attr, 'file_name') and attr.file_name:
                return attr.file_name
    if message.video:
        return f'video_{message.id}.mp4'
    if message.photo:
        return f'photo_{message.id}.jpg'
    return f'file_{message.id}'


def _get_video_attributes(message):
    """获取视频属性，用于保持流媒体播放"""
    if message.document:
        for attr in message.document.attributes:
            if isinstance(attr, DocumentAttributeVideo):
                return [DocumentAttributeVideo(
                    duration=attr.duration,
                    w=attr.w,
                    h=attr.h,
                    supports_streaming=True
                )]
    return None

async def _extract_random_frame(video_buffer):
    """从视频中随机抽取一帧作为缩略图"""
    import cv2
    import numpy as np
    import tempfile
    
    try:
        # 将 BytesIO 写入临时文件（OpenCV 需要文件路径）
        with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as tmp_file:
            tmp_path = tmp_file.name
            tmp_file.write(video_buffer.getvalue())
        
        # 打开视频
        cap = cv2.VideoCapture(tmp_path)
        if not cap.isOpened():
            return None
        
        # 获取总帧数
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if total_frames <= 0:
            cap.release()
            return None
        
        # 随机选择一帧（避免太开头或太结尾）
        # 选择 20% 到 80% 之间的随机帧
        start_frame = int(total_frames * 0.2)
        end_frame = int(total_frames * 0.8)
        random_frame = np.random.randint(start_frame, max(end_frame, start_frame + 1))
        
        # 跳到该帧
        cap.set(cv2.CAP_PROP_POS_FRAMES, random_frame)
        ret, frame = cap.read()
        cap.release()
        
        if not ret or frame is None:
            return None
        
        # 将帧转换为 JPEG
        success, encoded = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not success:
            return None
        
        # 转换为 BytesIO
        thumb_buffer = io.BytesIO(encoded.tobytes())
        thumb_buffer.seek(0)
        
        # 清理临时文件
        import os
        os.unlink(tmp_path)
        
        return thumb_buffer
        
    except Exception as e:
        logger.error(f'提取视频帧失败: {str(e)}')
        return None