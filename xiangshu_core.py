#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
象数功效查询核心模块
解析策略：PDF 每行格式为「症状描述：象数1或象数2…」
"""

import re
import os
import sys
import json
from typing import Dict, List, Optional, Tuple

try:
    import pdfplumber
    _PDF_BACKEND = "pdfplumber"
except ImportError:
    import PyPDF2
    _PDF_BACKEND = "pypdf2"


# 匹配行内「象数」段：数字（可含·分隔）开头，直到遇到汉字或句末符号
_XS_SEG = re.compile(r'(\d[\d·]*)')
# 去除页码标记，如 —8— 或 ─8─
_PAGE_MARKER = re.compile(r'^[—─\-–]+\d+[—─\-–]+')
# 候选分隔符（将一行内多组象数分开）
_ALT_SEP = re.compile(r'[或,，;；]')
# 判断象数是否「纯为数字+分隔符」，排除含汉字的噪音
_PURE_XS = re.compile(r'^[\d·\s]+$')

# 纯注释/元数据标签，不是症状
_NOISE_EXACT = frozenset({'方义', '治法', '象数配方', '配方', '注', '说明', '按语', '方例'})
# 症状中出现这些短语说明是说明性文字，非疾病描述
_NOISE_CONTAINS = re.compile(
    r'(，治法|，方义|，配方|或暂停|或停念|继续默念|改念|或念配方|用配方|'
    r'老师的配方|给的配方|[*×＊]|\d+[；;]\w+用配方)'
)
# 仅是TCM舌苔/脉象碎片（缺乏疾病主体）
_NOISE_TCM_DIAG = re.compile(r'^(苔[白黄厚薄腻]|舌[红淡暗]|脉[细数沉弱迟濡滑涩])')
# 含象数格式的假症状（症状里含数字·数字，是公式引用）
_NOISE_HAS_XS = re.compile(r'\d[\d·]{3,}\d')
# 带序号前缀，如 "3."、"5、"
_NUMBERED_PREFIX = re.compile(r'^\d+[\.、·]\s*')
# 有效备注需包含时效/效果词汇
_NOTE_KEYWORDS = re.compile(r'(见效|好了|见好|改善|消失|有效|分钟|秒|天后|立即|马上|当晚|当天|即好|即愈|奇效|痊愈|减轻|缓解)')

# 同义词扩展表：查询词 → 扩展词列表（OR逻辑命中任一即匹配）
SYNONYMS: Dict[str, List[str]] = {
    '咳': ['咳', '咳嗽'],
    '咳嗽': ['咳', '咳嗽'],
    '头疼': ['头疼', '头痛'],
    '头痛': ['头疼', '头痛'],
    '感冒': ['感冒', '伤风'],
    '胃疼': ['胃疼', '胃痛', '胃脘痛'],
    '胃痛': ['胃疼', '胃痛', '胃脘痛'],
    '腰疼': ['腰疼', '腰痛'],
    '腰痛': ['腰疼', '腰痛'],
    '肚子疼': ['肚子疼', '腹痛', '腹疼'],
    '腹痛': ['腹痛', '腹疼', '肚子疼'],
    '失眠': ['失眠', '不寐', '睡眠不好', '难以入睡'],
    '高血压': ['高血压', '血压高'],
    '糖尿病': ['糖尿病', '消渴'],
    '便秘': ['便秘', '大便秘结', '大便干'],
    '腹泻': ['腹泻', '泄泻', '拉肚子'],
}


def normalize_xiangshu(s: str) -> str:
    """将象数字符串规范化：去掉·，统一空格分隔。例如 '06·04·03' -> '06 04 03'"""
    s = s.replace('·', ' ').replace('.', ' ')
    return ' '.join(s.split())


def _clean_symptom(raw: str) -> Optional[str]:
    """
    清洗并验证症状字符串。返回 None 表示是噪音，应丢弃。
    处理：序号前缀、治法后缀、注释标签、句子碎片。
    """
    s = raw.strip()

    # 1. 去掉序号前缀：「3.」「5、」「（1）」等
    s = _NUMBERED_PREFIX.sub('', s).strip()
    s = re.sub(r'^[（(]\d+[）)]\s*', '', s).strip()

    # 2. 去掉末尾「，治法XXX」或「用配方」之类的注释后缀
    s = re.sub(r'[，,]\s*治法.*$', '', s).strip()
    s = re.sub(r'[，,]?\s*(用配方|改念|或念|的配方)\s*$', '', s).strip()

    # 3. 精确匹配噪音标签
    if s in _NOISE_EXACT:
        return None

    # 4. 含说明性短语（非疾病描述）
    if _NOISE_CONTAINS.search(s):
        return None

    # 5. 仅是TCM舌苔/脉象碎片
    if _NOISE_TCM_DIAG.match(s):
        return None

    # 6. 含象数格式（症状里夹了公式引用，是句子碎片）
    if _NOISE_HAS_XS.search(s):
        return None

    # 7. 必须含至少一个汉字
    if not re.search(r'[\u4e00-\u9fff]', s):
        return None

    # 8. 过短（单字）
    if len(s) < 2:
        return None

    return s


def _extract_xs_from_formula(formula_text: str) -> Tuple[List[str], str]:
    """
    从公式段（冒号之后的部分）提取所有象数及说明备注，例如：
      '650·3820或260·50·380或640·380'     -> (['650 3820', '260 50 380', '640 380'], '')
      '72000念几分钟见效'                  -> (['72000'], '念几分钟见效')
      '650·3820或260·380当晚就睡得香'     -> (['650 3820', '260 380'], '当晚就睡得香')
    返回：(xs_list, note)，note 为跟在最后一段公式后的汉字说明（可为空字符串）
    """
    results = []
    note = ''
    # 先按「或/，」切割候选片段
    candidates = _ALT_SEP.split(formula_text)
    for cand in candidates:
        cand = cand.strip()
        if not cand:
            continue
        # 取候选片段开头的数字+·序列（停在第一个汉字前）
        m = re.match(r'^([\d·]+)(.*)', cand)
        if m:
            raw = m.group(1).strip('·')   # 去首尾多余的·
            xs = normalize_xiangshu(raw)
            tail = m.group(2).strip()
            # 至少含一个数字，且长度合理（避免单字母/页码噪音）
            if xs and re.search(r'\d', xs):
                results.append(xs)
                # 尾部若含汉字说明，记为 note（取最长的那段）
                if tail and re.search(r'[\u4e00-\u9fff]', tail):
                    if len(tail) > len(note):
                        note = tail
    return results, note


def _parse_line(line: str, page_num: int, data: Dict):
    """解析单行，提取「症状 -> 象数」映射，更新 data 字典。"""
    # 去页码标记
    line = _PAGE_MARKER.sub('', line).strip()
    if not line:
        return

    # 找出行内所有「冒号（：/:）后紧跟数字」的位置
    # 使用 split 处理多个冒号情况（取最后一个有效的）
    # 支持中文冒号和英文冒号
    colon_re = re.compile(r'[：:]')
    parts = colon_re.split(line)

    # 逐段扫描：每段可能是「症状文字」，下一段可能是「象数公式」
    for i, part in enumerate(parts[:-1]):          # 最后一段后面没有冒号
        symptom_raw = part.strip()
        formula_raw = parts[i + 1].strip()

        # 公式段必须以数字开头（最多忽略前导空格）
        if not formula_raw or not re.match(r'^\d', formula_raw):
            continue

        # 清洗症状：取最后一句（去掉前面可能属于上一个条目的内容）
        # 按「。」分割，取最后非空片段
        symptom_segs = [s.strip() for s in re.split(r'[。！？\n]', symptom_raw) if s.strip()]
        symptom_raw_last = symptom_segs[-1] if symptom_segs else symptom_raw

        # 清洗并验证症状（过滤注释标签、序号、噪音片段）
        symptom = _clean_symptom(symptom_raw_last)
        if symptom is None:
            continue

        # 从公式段提取象数列表及备注
        xs_list, note = _extract_xs_from_formula(formula_raw)
        for xs in xs_list:
            if xs not in data:
                data[xs] = {'symptoms': [], 'notes': [], 'pages': set()}
            entry = data[xs]
            if symptom not in entry['symptoms']:
                entry['symptoms'].append(symptom)
            # 只保留含时效/效果词汇的有意义备注
            if note and _NOTE_KEYWORDS.search(note) and note not in entry['notes']:
                entry['notes'].append(note)
            entry['pages'].add(page_num)


class XiangShuQuery:
    """象数功效查询类（重构版）"""

    def __init__(self, pdf_path: str, cache_path: Optional[str] = None):
        self.pdf_path = pdf_path
        self.cache_path = cache_path or (pdf_path + ".cache.json")
        # xiangshu -> {symptoms: [str], pages: [int], content: str}
        self.xiangshu_data: Dict[str, Dict] = {}
        self._load()

    # ------------------------------------------------------------------ #
    #  加载（优先读缓存）
    # ------------------------------------------------------------------ #

    def _load(self):
        if self._try_load_cache():
            print(f"[缓存] 已加载 {len(self.xiangshu_data)} 条象数（跳过 PDF 解析）", file=sys.stderr)
            return
        print(f"[解析] 正在解析 PDF：{self.pdf_path}", file=sys.stderr)
        raw = self._parse_pdf()
        # 后处理：将 pages set 转为排序列表，生成 content 字段
        for xs, entry in raw.items():
            pages = sorted(entry['pages'])
            symptoms = entry['symptoms']
            notes = entry.get('notes', [])
            self.xiangshu_data[xs] = {
                'symptoms': symptoms,
                'notes': notes,
                'pages': pages,
                'content': '；'.join(symptoms),
            }
        print(f"[解析] 完成，共 {len(self.xiangshu_data)} 条象数", file=sys.stderr)
        self._save_cache()

    # ------------------------------------------------------------------ #
    #  PDF 解析
    # ------------------------------------------------------------------ #

    def _parse_pdf(self) -> Dict:
        raw: Dict = {}
        if _PDF_BACKEND == "pdfplumber":
            self._parse_pdfplumber(raw)
        else:
            self._parse_pypdf2(raw)
        return raw

    def _parse_pdfplumber(self, raw: Dict):
        import pdfplumber
        with pdfplumber.open(self.pdf_path) as pdf:
            total = len(pdf.pages)
            print(f"  PDF 共 {total} 页", file=sys.stderr)
            for page_num, page in enumerate(pdf.pages):
                text = page.extract_text()
                if text:
                    for line in text.split('\n'):
                        _parse_line(line, page_num, raw)

    def _parse_pypdf2(self, raw: Dict):
        import PyPDF2
        with open(self.pdf_path, 'rb') as f:
            reader = PyPDF2.PdfReader(f)
            total = len(reader.pages)
            print(f"  PDF 共 {total} 页（PyPDF2 模式）", file=sys.stderr)
            for page_num in range(total):
                text = reader.pages[page_num].extract_text() or ''
                for line in text.split('\n'):
                    _parse_line(line, page_num, raw)

    # ------------------------------------------------------------------ #
    #  缓存
    # ------------------------------------------------------------------ #

    def _try_load_cache(self) -> bool:
        if not os.path.exists(self.cache_path):
            return False
        try:
            pdf_mtime = os.path.getmtime(self.pdf_path)
            cache_mtime = os.path.getmtime(self.cache_path)
            if cache_mtime < pdf_mtime:
                print("[缓存] PDF 已更新，重新解析")
                return False
            with open(self.cache_path, 'r', encoding='utf-8') as f:
                cached = json.load(f)
            if cached.get('version') != 2:
                return False
            self.xiangshu_data = cached['data']
            return True
        except Exception as e:
            print(f"[缓存] 读取失败：{e}", file=sys.stderr)
            return False

    def _save_cache(self):
        try:
            with open(self.cache_path, 'w', encoding='utf-8') as f:
                json.dump({'version': 2, 'data': self.xiangshu_data}, f,
                          ensure_ascii=False, separators=(',', ':'))
            print(f"[缓存] 已保存至 {self.cache_path}", file=sys.stderr)
        except Exception as e:
            print(f"[缓存] 保存失败：{e}", file=sys.stderr)

    # ------------------------------------------------------------------ #
    #  从预解析 JSON 加载（无需 PDF）
    # ------------------------------------------------------------------ #

    @classmethod
    def load_from_json(cls, json_path: str) -> 'XiangShuQuery':
        """从预解析的 xiangshu_data.json 创建实例，无需 PDF 文件。"""
        obj = object.__new__(cls)
        obj.pdf_path = ''
        obj.cache_path = ''
        obj.xiangshu_data = {}
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict) and 'data' in data:
            obj.xiangshu_data = data['data']
        else:
            obj.xiangshu_data = data
        print(f"[JSON] 已加载 {len(obj.xiangshu_data)} 条象数", file=sys.stderr)
        return obj

    # ------------------------------------------------------------------ #
    #  查询接口
    # ------------------------------------------------------------------ #

    def search_by_number(self, xiangshu: str) -> Optional[Dict]:
        """按象数精确查询（先规范化再查）"""
        xs = normalize_xiangshu(xiangshu)
        return self.xiangshu_data.get(xs)

    def search_by_keyword(self, keyword: str) -> List[Tuple[str, Dict]]:
        """
        按关键词搜索症状内容，结果按命中次数倒排。
        支持多关键词（空格分隔，AND 逻辑）。
        支持同义词扩展（每个关键词扩展为一组候选，命中任一即可）。
        结果中包含 matched_symptoms：仅展示与查询词相关的症状。
        """
        keywords = keyword.strip().split()
        if not keywords:
            return []

        # 将每个关键词扩展为候选词组（OR）
        expanded: List[List[str]] = [SYNONYMS.get(kw, [kw]) for kw in keywords]

        results = []
        for xs, entry in self.xiangshu_data.items():
            content = entry.get('content', '')
            # AND 逻辑：每组候选中至少有一个命中
            if not all(any(syn in content for syn in group) for group in expanded):
                continue
            # 相关度 = 所有候选词出现次数之和
            score = sum(
                content.count(syn)
                for group in expanded
                for syn in group
            )
            # 只保留与查询词相关的症状（任意候选词命中）
            all_syns = [syn for group in expanded for syn in group]
            matched = [s for s in entry.get('symptoms', [])
                       if any(syn in s for syn in all_syns)]
            result_entry = dict(entry)
            result_entry['matched_symptoms'] = matched
            results.append((xs, result_entry, score))

        results.sort(key=lambda x: x[2], reverse=True)
        return [(xs, entry) for xs, entry, _ in results]

    def search_by_symptom(self, symptom: str) -> List[Tuple[str, Dict]]:
        return self.search_by_keyword(symptom)

    def list_all(self, limit: int = 50) -> List[Tuple[str, Dict]]:
        return list(self.xiangshu_data.items())[:limit]
