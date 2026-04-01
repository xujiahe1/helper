import json
import os
import re
import time
import uuid
from typing import Optional

import requests


def _parse_wave_uri(uri: str) -> Optional[str]:
    """从 wave://ref_id/filename 中提取 ref_id，失败返回 None"""
    if not uri.startswith('wave://'):
        return None
    rest = uri[len('wave://'):]
    parts = rest.strip('/').split('/')
    return parts[0] if parts and parts[0] else None


class KmDoc:
    """KM 文档 JSON 结构的封装，提供清理、简化、还原等功能"""

    # 编辑器内部属性，对文档内容没有意义，可安全去掉
    STRIP_ATTR_KEYS = {
        'tracked-author', 'mark-tracked', 'tracked-color',
        'node-id',
        'auto-composed-image-block',
        'blockMark', 'group-child',
        'class', 'headingClass',
        'counterStyle',
        'commentIds', 'resolvedIds',
        'minWidth',
    }

    # 类级别缓存
    _wave_file_refs: dict[str, dict[str, str]] = {}   # ref_id -> {url, filename}
    _card_store: dict[str, dict] = {}                  # ref_id -> 原始完整 card 数据
    _card_key_map: dict[str, str] = {}                 # card identity key -> ref_id（反向索引）
    _at_store: dict[str, dict] = {}                    # name -> 原始 at attrs

    def __init__(self, doc: dict):
        self._doc = doc

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------

    @classmethod
    def from_json(cls, s: str) -> 'KmDoc':
        return cls(json.loads(s))

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def doc(self) -> dict:
        return self._doc

    # ------------------------------------------------------------------
    # 清理 / 填充
    # ------------------------------------------------------------------

    def clean(self) -> 'KmDoc':
        """去除编辑器内部冗余字段（原地修改），返回 self 以支持链式调用"""
        _clean_node(self._doc, self.STRIP_ATTR_KEYS)
        return self

    def fill_card_urls(self, doc_id: str, token: str, domain: str) -> 'KmDoc':
        """为所有 image/video card 刷新临时下载地址，结果自动注册到类级缓存。

        只往 _wave_file_refs 写入映射，不修改 card 本身的 source_url，
        避免 wave:// 临时地址被 restore 写回服务端。
        """
        cards: list[dict] = []
        _collect_media_cards(self._doc.get('content', []), cards)
        if not cards:
            return self
        api_url = f"https://{domain}/openapi/docs/v1/doc/document/card/get"
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": token,
        }
        for i, card in enumerate(cards):
            source_id = card.get('source_id', '')
            if not source_id:
                continue
            if i > 0:
                time.sleep(0.5)
            try:
                resp = requests.post(api_url, headers=headers,
                                     json={"source_id": source_id, "doc_id": doc_id},
                                     timeout=30)
                resp.raise_for_status()
                data = resp.json().get('data', {})
                download_url = data.get('file_url') or data.get('url') or data.get('download_url') or ''
                if download_url:
                    file_info = card.get('file', {})
                    filename = file_info.get('file_name', 'image')
                    key = f"image:{source_id or card.get('card_id', '')}"
                    ref_id = _get_or_create_ref_id(key)
                    KmDoc._wave_file_refs[ref_id] = {'url': download_url, 'filename': filename}
            except Exception:
                pass
        return self

    # ------------------------------------------------------------------
    # 简化 / 还原 — 让 AI 不直接接触复杂的 card 结构
    # ------------------------------------------------------------------

    def simplify(self) -> 'KmDoc':
        """将 image/video card 替换为简化的 image_ref/video_ref 节点。

        原始 card 数据存入类级 _card_store，还原时可恢复。
        """
        _simplify_nodes(self._doc.get('content', []))
        return self

    def upload_new_images(self, token: str, domain: str) -> 'KmDoc':
        """扫描文档中新增的 image_ref 和 video_ref，自动中转上传到 KM 存储。

        调用时机：apply_edit 之后、restore 之前。
        """
        content = self._doc.get('content', [])
        img_refs = _collect_new_image_refs(content)
        vid_refs = _collect_new_video_refs(content)
        all_refs = [(r, 'image') for r in img_refs] + [(r, 'video') for r in vid_refs]
        if not all_refs:
            return self

        upload_link_url = f"https://{domain}/openapi/docs/v1/file/upload_link/get"
        headers = {"Content-Type": "application/json", "Authorization": token}

        for ref, media_type in all_refs:
            url = ref.get('url', '')
            filename = ref.get('filename', '') or ('image.png' if media_type == 'image' else 'video.mp4')
            try:
                if os.path.isfile(url):
                    with open(url, 'rb') as f:
                        file_data = f.read()
                    import mimetypes
                    ct = mimetypes.guess_type(url)[0] or ('image/png' if media_type == 'image' else 'video/mp4')
                    filename = os.path.basename(url)
                elif media_type == 'video':
                    continue  # 视频不支持从网络 URL 下载，太大
                else:
                    img_resp = requests.get(url, allow_redirects=True, timeout=30)
                    img_resp.raise_for_status()
                    file_data = img_resp.content
                    ct = img_resp.headers.get('content-type', 'image/png')

                time.sleep(0.5)
                link_resp = requests.post(
                    upload_link_url, headers=headers,
                    json={"file_name": filename}, timeout=30,
                ).json()
                if link_resp.get("retcode") != 0:
                    continue
                link_data = link_resp["data"]

                time.sleep(0.5)
                upload_timeout = 300 if media_type == 'video' else 60
                requests.post(
                    link_data["upload_link"],
                    data=link_data["upload_param"],
                    files={'file': (filename, file_data, ct)},
                    timeout=upload_timeout,
                )
                ref['url'] = link_data["file_url"]
            except Exception:
                pass
        return self

    def restore(self) -> 'KmDoc':
        """将 image_ref/video_ref 节点还原为完整的 card 节点。

        - 有 ref_id 且在 _card_store 中 → 恢复原始 card
        - 有 url 但无 ref_id → 创建最小新 card（用于新增图片）
        - 无 url 也无 ref_id → 无法还原，保留原样
        """
        _restore_nodes(self._doc.get('content', []))
        return self

    # ------------------------------------------------------------------
    # wave 文件引用
    # ------------------------------------------------------------------

    @staticmethod
    def resolve_ref(ref_path: str) -> Optional[dict[str, str]]:
        """解析 wave://ref_id/filename 引用路径，返回 {url, filename} 或 None"""
        ref_id = _parse_wave_uri(ref_path)
        if ref_id is None:
            return None
        return KmDoc._wave_file_refs.get(ref_id)

    @staticmethod
    def download_ref(ref_path: str, save_path: Optional[str] = None) -> tuple[Optional[bytes], str]:
        """下载引用对应的文件。支持 wave://、http(s)://、本地路径。

        Returns:
            (file_bytes, message) — 若 save_path 指定则写入文件并返回 (None, ok_msg)，
            否则返回 (bytes, filename)。出错时返回 (None, error_msg)。
        """

        if ref_path.startswith('wave://'):
            ref = KmDoc.resolve_ref(ref_path)
            if ref is None:
                return None, f"引用未找到: {ref_path}，可能已过期，请重新获取文档"
            url = ref['url']
            filename = ref['filename']
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
            file_bytes = resp.content
        elif ref_path.startswith(('http://', 'https://')):
            resp = requests.get(ref_path, allow_redirects=True, timeout=60)
            resp.raise_for_status()
            file_bytes = resp.content
            filename = ref_path.rsplit('/', 1)[-1].split('?')[0] or 'image.png'
        elif os.path.isfile(ref_path):
            with open(ref_path, 'rb') as f:
                file_bytes = f.read()
            filename = os.path.basename(ref_path)
        else:
            return None, f"无法识别的路径: {ref_path}（支持 wave://、http(s)://、本地路径）"

        if save_path:
            with open(save_path, 'wb') as f:
                f.write(file_bytes)
            return None, f"已下载到 {save_path} ({len(file_bytes)} bytes)"

        return file_bytes, filename

    # ------------------------------------------------------------------
    # 锚点
    # ------------------------------------------------------------------

    def add_anchors(self) -> 'KmDoc':
        """为每个顶层 block 添加多层级 _anchor 字符串，反映文档的标题层次结构。

        heading 节点产生新的层级（如 "3.2"），其后的内容节点在其下编号（如 "3.2.1"）。
        """
        blocks = self._doc.get('content', [])
        # stack: [[heading_level, child_counter], ...]
        # virtual root at level 0
        stack: list[list[int]] = [[0, 0]]

        for block in blocks:
            if not isinstance(block, dict):
                continue

            is_heading = block.get('type') == 'heading'

            if is_heading:
                level = block.get('attrs', {}).get('level', 1)
                # 弹出同级或更深的 scope
                while len(stack) > 1 and stack[-1][0] >= level:
                    stack.pop()

            # 当前 scope 计数 +1
            stack[-1][1] += 1
            anchor = '.'.join(str(s[1]) for s in stack)
            # 确保 _anchor 在 dict 最前面
            items = list(block.items())
            block.clear()
            block['_anchor'] = anchor
            block.update(items)

            if is_heading:
                level = block.get('attrs', {}).get('level', 1)
                stack.append([level, 0])

        return self

    def strip_anchors(self) -> 'KmDoc':
        """移除所有顶层 block 上的 _anchor 字段"""
        for block in self._doc.get('content', []):
            if isinstance(block, dict):
                block.pop('_anchor', None)
        return self

    # ------------------------------------------------------------------
    # 锚点编辑
    # ------------------------------------------------------------------

    def apply_edit(self, anchor: str, action: str, content) -> 'KmDoc':
        """基于锚点执行编辑操作。

        Args:
            anchor: 目标 block 的 _anchor 值（多层级字符串，如 "3.2.1"）
            action: 操作类型
                - replace  : 用 content 替换锚点处的 block
                - insert_after  : 在锚点后面插入 content
                - insert_before : 在锚点前面插入 content
                - delete   : 删除锚点处的 block（content 忽略）
            content: dict（单个 block）或 list[dict]（多个 block）
        """
        blocks = self._doc.get('content', [])

        # 找到锚点对应的索引
        idx = None
        for i, block in enumerate(blocks):
            if isinstance(block, dict) and block.get('_anchor') == anchor:
                idx = i
                break
        if idx is None:
            raise ValueError(f"anchor {anchor!r} not found")

        # 统一 content 为 list
        if content is None:
            new_blocks = []
        elif isinstance(content, dict):
            new_blocks = [content]
        elif isinstance(content, list):
            new_blocks = content
        else:
            new_blocks = [content]

        if action == 'replace':
            blocks[idx:idx + 1] = new_blocks
        elif action == 'insert_after':
            blocks[idx + 1:idx + 1] = new_blocks
        elif action == 'insert_before':
            blocks[idx:idx] = new_blocks
        elif action == 'delete':
            del blocks[idx]
        else:
            raise ValueError(f"unknown action: {action}")

        return self

    # ------------------------------------------------------------------
    # 纯文本提取
    # ------------------------------------------------------------------

    def to_plain_text(self) -> str:
        """将文档转为纯文本格式，保留图片/视频/KM链接引用。

        大幅压缩 token 占用，适合只需阅读内容而不需要编辑的场景。
        """
        lines: list[str] = []
        for block in self._doc.get('content', []):
            if not isinstance(block, dict):
                continue
            anchor = block.get('_anchor', '')
            prefix = f"[{anchor}] " if anchor else ''
            _block_to_plain(block, lines, prefix, indent_level=0)
        return '\n'.join(lines)

    # ------------------------------------------------------------------
    # 输出
    # ------------------------------------------------------------------

    def to_json(self, **kwargs) -> str:
        return json.dumps(self._doc, ensure_ascii=False, **kwargs)


# ======================================================================
# 内部工具函数 — 清理
# ======================================================================

def _clean_node(node: dict, strip_keys: set):
    """递归清理 KM JSON 节点中的编辑器冗余字段"""
    if not isinstance(node, dict):
        return

    attrs = node.get('attrs')
    if isinstance(attrs, dict):
        for key in list(attrs.keys()):
            if key in strip_keys:
                del attrs[key]
        nested_marks = attrs.get('marks')
        if isinstance(nested_marks, list):
            attrs['marks'] = [m for m in nested_marks if m.get('type') != 'track']
            if not attrs['marks']:
                del attrs['marks']
        if not attrs:
            del node['attrs']

    marks = node.get('marks')
    if isinstance(marks, list):
        node['marks'] = [m for m in marks if m.get('type') != 'track']
        for m in node.get('marks', []):
            m_attrs = m.get('attrs')
            if isinstance(m_attrs, dict):
                # 去掉 null 值和默认的 target/_blank、class
                for k in list(m_attrs.keys()):
                    if m_attrs[k] is None or k in ('target', 'class'):
                        del m_attrs[k]
                if not m_attrs:
                    del m['attrs']
        if not node['marks']:
            del node['marks']

    content = node.get('content')
    if isinstance(content, list):
        for child in content:
            _clean_node(child, strip_keys)


def _collect_media_cards(nodes: list, result: list):
    """递归收集所有 image / video card，无论是否已有 source_url。"""
    for node in nodes:
        if not isinstance(node, dict):
            continue
        card = node.get('attrs', {}).get('card', {})
        if card and card.get('type') in ('image', 'video'):
            result.append(card)
        content = node.get('content', [])
        if isinstance(content, list):
            _collect_media_cards(content, result)


_KM_OSS_PREFIX = 'https://miobjectbiz.mihoyo.com/'


def _collect_new_image_refs(nodes: list) -> list[dict]:
    """递归收集需要中转上传的新 image_ref 节点（外部 URL / 本地路径，非 wave:// 非 KM OSS）"""
    result: list[dict] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get('type') == 'image_ref':
            ref_id = node.get('ref_id', '')
            url = node.get('url', '')
            if (url
                    and not url.startswith('wave://')
                    and not url.startswith(_KM_OSS_PREFIX)
                    and not KmDoc._card_store.get(ref_id)):
                result.append(node)
        content = node.get('content')
        if isinstance(content, list):
            result.extend(_collect_new_image_refs(content))
    return result


def _collect_new_video_refs(nodes: list) -> list[dict]:
    """递归收集需要中转上传的新 video_ref 节点（本地路径 / 外部 URL）"""
    result: list[dict] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get('type') == 'video_ref':
            ref_id = node.get('ref_id', '')
            url = node.get('url', '')
            if (url
                    and not url.startswith('wave://')
                    and not url.startswith(_KM_OSS_PREFIX)
                    and not KmDoc._card_store.get(ref_id)):
                result.append(node)
        content = node.get('content')
        if isinstance(content, list):
            result.extend(_collect_new_video_refs(content))
    return result


# ======================================================================
# 内部工具函数 — 简化 / 还原
# ======================================================================

def _simplify_nodes(nodes: list):
    """遍历节点数组，原地将 card/at 节点替换为简化节点，并合并相邻文本"""
    for i, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        n_type = node.get('type')

        # 行内 image card → image_ref
        if n_type == 'inlineBlockCard':
            card = node.get('attrs', {}).get('card', {})
            if card.get('type') == 'image':
                nodes[i] = _card_to_image_ref(card)
                continue
            if card.get('type') == 'km-article':
                nodes[i] = _card_to_km_link(card, node)
                continue

        # 块级 card（video 等）→ video_ref
        if n_type == 'card':
            card = node.get('attrs', {}).get('card', {})
            if card.get('type') == 'video':
                nodes[i] = _card_to_video_ref(card, node.get('attrs', {}))
                continue

        # 递归处理子节点
        content = node.get('content')
        if isinstance(content, list):
            _simplify_nodes(content)
            # at → text，合并相邻文本
            _flatten_at_and_merge(content)


def _get_or_create_ref_id(key: str) -> str:
    """根据 card 标识 key 获取已有 ref_id，或生成新的"""
    existing = KmDoc._card_key_map.get(key)
    if existing:
        return existing
    ref_id = uuid.uuid4().hex[:8]
    KmDoc._card_key_map[key] = ref_id
    return ref_id


def _card_to_image_ref(card: dict) -> dict:
    """将完整 image card 转为简化的 image_ref 节点，原始数据存入 _card_store"""
    key = f"image:{card.get('source_id') or card.get('card_id', '')}"
    ref_id = _get_or_create_ref_id(key)
    KmDoc._card_store[ref_id] = card.copy()

    file_info = card.get('file', {})
    props = card.get('props', {})
    filename = file_info.get('file_name', 'image')
    source_url = f'wave://{ref_id}/{filename}'

    return {
        'type': 'image_ref',
        'ref_id': ref_id,
        'filename': filename,
        'width': props.get('width', ''),
        'height': props.get('height', ''),
        'url': source_url,
    }


def _card_to_video_ref(card: dict, block_attrs: dict) -> dict:
    """将完整 video card 转为简化的 video_ref 节点"""
    key = f"video:{card.get('source_id') or card.get('card_id', '')}"
    ref_id = _get_or_create_ref_id(key)
    KmDoc._card_store[ref_id] = {'_block_attrs': block_attrs, **card.copy()}

    file_info = card.get('file', {})
    filename = file_info.get('file_name', '')
    ref: dict = {
        'type': 'video_ref',
        'ref_id': ref_id,
        'filename': filename,
        'url': f'wave://{ref_id}/{filename}',
    }
    size = file_info.get('file_size')
    if size:
        ref['size'] = size
    return ref


def _card_to_km_link(card: dict, node: dict) -> dict:
    """将 km-article inlineBlockCard 转为简化的 km_link 节点"""
    props = card.get('props', {})
    key = f"km:{props.get('href', '') or card.get('card_id', '')}"
    ref_id = _get_or_create_ref_id(key)
    KmDoc._card_store[ref_id] = node.copy()

    href = props.get('href', '')
    kmid = href.split('/doc/')[-1].split('/')[0].split('?')[0] if '/doc/' in href else ''
    title = props.get('hrefTitle', '')

    result: dict = {
        'type': 'km_link',
        'ref_id': ref_id,
        'kmid': kmid,
        'title': title,
    }
    marks = node.get('marks')
    if marks:
        result['marks'] = marks
    return result


def _flatten_at_and_merge(nodes: list):
    """将 at 节点转为 @name 文本，再合并相邻的同 marks 文本节点（原地修改）"""
    changed = False
    for i, node in enumerate(nodes):
        if isinstance(node, dict) and node.get('type') == 'at':
            attrs = node.get('attrs', {})
            name = attrs.get('name', '')
            if name:
                KmDoc._at_store[name] = attrs.copy()
                nodes[i] = {'type': 'text', 'text': f'@{name}'}
                changed = True

    if not changed:
        return

    # 合并相邻的文本节点（marks 相同时）
    merged: list[dict] = []
    for node in nodes:
        if (isinstance(node, dict) and node.get('type') == 'text'
                and merged and merged[-1].get('type') == 'text'
                and _same_marks(merged[-1], node)):
            merged[-1]['text'] += node['text']
        else:
            merged.append(node)
    nodes[:] = merged


def _same_marks(a: dict, b: dict) -> bool:
    """判断两个节点的 marks 是否完全相同"""
    ma = a.get('marks')
    mb = b.get('marks')
    if not ma and not mb:
        return True
    return ma == mb


def _restore_nodes(nodes: list):
    """遍历节点数组，原地将 ref 节点还原为完整 card 节点，并还原 @name → at"""
    for i, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        n_type = node.get('type')

        if n_type == 'image_ref':
            nodes[i] = _image_ref_to_card(node)
            continue

        if n_type == 'video_ref':
            nodes[i] = _video_ref_to_card(node)
            continue

        if n_type == 'km_link':
            nodes[i] = _km_link_to_card(node)
            continue

        content = node.get('content')
        if isinstance(content, list):
            _restore_nodes(content)
            _restore_at_in_content(content)


def _resolve_ref_url(url: str) -> str:
    """将 wave:// URL 还原为真实下载地址，非 wave:// 原样返回。"""
    ref_id = _parse_wave_uri(url)
    if ref_id is None:
        return url
    entry = KmDoc._wave_file_refs.get(ref_id)
    return entry['url'] if entry else ''


def _image_ref_to_card(ref: dict) -> dict:
    """将 image_ref 还原为 inlineBlockCard 节点"""
    ref_id = ref.get('ref_id', '')
    stored = KmDoc._card_store.get(ref_id)

    if stored:
        card = stored.copy()
        url = ref.get('url', '')
        if url and not url.startswith('wave://'):
            card['source_url'] = url
        elif url.startswith('wave://'):
            card['source_url'] = _resolve_ref_url(url) or card.get('source_url', '')
        return {
            'type': 'inlineBlockCard',
            'attrs': {'card': card},
        }

    # 新增图片：构造最小 card
    url = _resolve_ref_url(ref.get('url', ''))
    card: dict = {
        'type': 'image',
        'card_id': '',
        'source_id': '',
        'id': '',
        'source_url': url,
        'object_id': 0,
        'enc_object_id': '',
        'object_type': 1,
    }
    width = ref.get('width', '')
    height = ref.get('height', '')
    if width or height:
        card['props'] = {'width': width, 'height': height}
    filename = ref.get('filename', '')
    if filename:
        ext = filename.rsplit('.', 1)[-1] if '.' in filename else ''
        card['file'] = {'file_name': filename, 'file_ext': ext}
    return {
        'type': 'inlineBlockCard',
        'attrs': {'card': card},
    }


def _video_ref_to_card(ref: dict) -> dict:
    """将 video_ref 还原为 card 块节点"""
    ref_id = ref.get('ref_id', '')
    stored = KmDoc._card_store.get(ref_id)

    if stored:
        block_attrs = stored.pop('_block_attrs', {})
        card = stored.copy()
        block_attrs['card'] = card
        return {
            'type': 'card',
            'attrs': block_attrs,
        }

    url = _resolve_ref_url(ref.get('url', ''))
    return {
        'type': 'card',
        'attrs': {
            'indent': 0,
            'card': {
                'type': 'video',
                'card_id': '',
                'source_id': '',
                'id': '',
                'source_url': url,
                'object_id': 0,
                'enc_object_id': '',
                'object_type': 1,
                'file': {
                    'file_name': ref.get('filename', ''),
                    'file_size': ref.get('size', 0),
                    'file_ext': ref.get('filename', '').rsplit('.', 1)[-1] if '.' in ref.get('filename', '') else '',
                },
            }
        },
    }


def _km_link_to_card(ref: dict) -> dict:
    """将 km_link 还原为 inlineBlockCard 节点"""
    ref_id = ref.get('ref_id', '')
    stored = KmDoc._card_store.get(ref_id)

    if stored:
        return stored.copy()

    # AI 新建的 km_link：构造最小 card
    kmid = ref.get('kmid', '')
    title = ref.get('title', '')
    href = f'https://km.mihoyo.com/doc/{kmid}' if kmid else ''
    card = {
        'type': 'km-article',
        'card_id': '',
        'source_id': '',
        'id': '',
        'source_url': '',
        'object_id': 0,
        'enc_object_id': '0',
        'object_type': 1,
        'poster_id': '',
        'props': {
            'hrefTitle': title,
            'href': href,
            'displayMode': 'preview',
            'articleType': 8,
        },
        'file': {'file_name': '', 'file_size': 0, 'file_type': ''},
    }
    result: dict = {
        'type': 'inlineBlockCard',
        'attrs': {'card': card},
    }
    marks = ref.get('marks')
    if marks:
        result['marks'] = marks
    return result


# ======================================================================
# 内部工具函数 — 纯文本提取
# ======================================================================

def _inline_to_text(nodes: list) -> str:
    """将行内节点数组提取为纯文本字符串，保留图片/视频/KM链接引用"""
    parts: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        n_type = node.get('type', '')
        if n_type == 'text':
            text = node.get('text', '')
            # 如果有链接 mark，附加 URL
            marks = node.get('marks', [])
            link_href = ''
            for m in marks:
                if m.get('type') == 'link':
                    link_href = m.get('attrs', {}).get('href', '')
            if link_href and link_href not in text:
                parts.append(f'{text}({link_href})')
            else:
                parts.append(text)
        elif n_type == 'image_ref':
            url = node.get('url', '')
            fn = node.get('filename', '')
            parts.append(f'[图片: {fn or url}]')
        elif n_type == 'video_ref':
            fn = node.get('filename', '')
            parts.append(f'[视频: {fn}]')
        elif n_type == 'km_link':
            title = node.get('title', '')
            kmid = node.get('kmid', '')
            parts.append(f'[KM链接: {title}({kmid})]')
        elif n_type == 'mathematics':
            expr = node.get('attrs', {}).get('data-content', '')
            parts.append(f'${expr}$')
        elif n_type == 'hardBreak':
            parts.append('\n')
    return ''.join(parts)


def _block_to_plain(block: dict, lines: list[str], prefix: str, indent_level: int):
    """将单个块节点转为纯文本行，追加到 lines"""
    b_type = block.get('type', '')
    indent = '  ' * indent_level
    content = block.get('content', [])

    if b_type == 'heading':
        level = block.get('attrs', {}).get('level', 1)
        text = _inline_to_text(content)
        lines.append(f'{prefix}{"#" * level} {text}')

    elif b_type == 'paragraph':
        text = _inline_to_text(content)
        lines.append(f'{prefix}{indent}{text}')

    elif b_type == 'listNew':
        attrs = block.get('attrs', {})
        li = attrs.get('listIndent', 1)
        kind = attrs.get('kind', 'bullet')
        checked = attrs.get('checked', False)
        sub_indent = '  ' * (li - 1)
        marker = '-' if kind == 'bullet' else '1.'
        if attrs.get('checked') is not None and kind == 'bullet':
            marker = '[x]' if checked else '[ ]'
        text = _inline_to_text(content)
        lines.append(f'{prefix}{sub_indent}{marker} {text}')

    elif b_type == 'codeBlock':
        lang = block.get('attrs', {}).get('language', '')
        text = _inline_to_text(content)
        lines.append(f'{prefix}```{lang}')
        lines.append(text)
        lines.append('```')

    elif b_type == 'horizontalRule':
        lines.append(f'{prefix}---')

    elif b_type == 'table':
        _table_to_plain(content, lines, prefix)

    elif b_type == 'expansion':
        for child in content:
            if not isinstance(child, dict):
                continue
            ct = child.get('type', '')
            if ct == 'expansion_summary':
                text = _inline_to_text(child.get('content', []))
                lines.append(f'{prefix}▸ {text}')
            elif ct == 'expansion_content':
                for sub in child.get('content', []):
                    if isinstance(sub, dict):
                        _block_to_plain(sub, lines, f'{prefix}  ', indent_level)

    elif b_type == 'tooltip':
        icon = block.get('attrs', {}).get('icon', '💡')
        for child in content:
            if not isinstance(child, dict):
                continue
            ct = child.get('type', '')
            if ct == 'tooltip_title':
                text = _inline_to_text(child.get('content', []))
                if text:
                    lines.append(f'{prefix}{icon} {text}')
            elif ct == 'tooltip_content':
                for sub in child.get('content', []):
                    if isinstance(sub, dict):
                        _block_to_plain(sub, lines, f'{prefix}{icon} ', indent_level)

    elif b_type == 'image_ref':
        url = block.get('url', '')
        fn = block.get('filename', '')
        lines.append(f'{prefix}[图片: {fn or url}]')

    elif b_type == 'video_ref':
        fn = block.get('filename', '')
        lines.append(f'{prefix}[视频: {fn}]')

    else:
        # 兜底：尝试提取文本
        text = _inline_to_text(content)
        if text:
            lines.append(f'{prefix}{indent}{text}')


def _table_to_plain(rows: list, lines: list[str], prefix: str):
    """将表格转为简单的文本表格"""
    table_data: list[list[str]] = []
    for row in rows:
        if not isinstance(row, dict) or row.get('type') != 'tableRow':
            continue
        cells: list[str] = []
        for cell in row.get('content', []):
            if not isinstance(cell, dict):
                continue
            cell_text = _inline_to_text(cell.get('content', [{}])[0].get('content', []) if cell.get('content') else [])
            # 尝试从 cell 内所有 paragraph 提取
            cell_lines: list[str] = []
            for para in cell.get('content', []):
                if isinstance(para, dict):
                    cell_lines.append(_inline_to_text(para.get('content', [])))
            cells.append(' '.join(cell_lines).strip())
        table_data.append(cells)

    if not table_data:
        return

    # 计算列宽
    max_cols = max(len(r) for r in table_data)
    col_widths = [0] * max_cols
    for row in table_data:
        for j, cell in enumerate(row):
            col_widths[j] = max(col_widths[j], len(cell))

    for i, row in enumerate(table_data):
        padded = [cell.ljust(col_widths[j]) if j < len(col_widths) else cell for j, cell in enumerate(row)]
        lines.append(f'{prefix}| {" | ".join(padded)} |')
        if i == 0:
            sep = ['-' * w for w in col_widths]
            lines.append(f'{prefix}| {" | ".join(sep)} |')


def _restore_at_in_content(nodes: list):
    """扫描文本节点中的 @name 模式，还原为 at 节点"""
    if not KmDoc._at_store:
        return

    names = sorted(KmDoc._at_store.keys(), key=len, reverse=True)
    pattern = '(' + '|'.join(re.escape(f'@{n}') for n in names) + ')'

    new_nodes: list[dict] = []
    changed = False

    for node in nodes:
        if not isinstance(node, dict) or node.get('type') != 'text':
            new_nodes.append(node)
            continue

        text = node.get('text', '')
        if not any(f'@{n}' in text for n in names):
            new_nodes.append(node)
            continue

        marks = node.get('marks')
        parts = re.split(pattern, text)

        for part in parts:
            if not part:
                continue
            if part.startswith('@') and part[1:] in KmDoc._at_store:
                new_nodes.append({
                    'type': 'at',
                    'attrs': KmDoc._at_store[part[1:]].copy(),
                })
                changed = True
            else:
                n: dict = {'type': 'text', 'text': part}
                if marks:
                    n['marks'] = [m.copy() for m in marks]
                new_nodes.append(n)

    if changed:
        nodes[:] = new_nodes
