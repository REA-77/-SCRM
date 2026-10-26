# -*- coding: utf-8 -*-
"""从运营部工作簿创建并导入平台运营账号；已有数据时拒绝重复导入。"""
import argparse
import json
import re
from collections import Counter
from decimal import Decimal


DDL = """
CREATE TABLE IF NOT EXISTS `平台运营账号` (
    `平台` VARCHAR(64) NOT NULL,
    `昵称` VARCHAR(255) NULL,
    `cookie` LONGTEXT NULL,
    `粉丝` BIGINT UNSIGNED NULL,
    `ID` VARCHAR(255) NULL,
    `主页链接` VARCHAR(1000) NULL,
    `Bit窗口` VARCHAR(255) NULL,
    INDEX `idx_platform_id` (`平台`, `ID`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='平台运营账号'
"""

ID_FIELDS = {
    'B站': ('B站ID',),
    '抖音': ('抖音ID-IT用', '抖音ID', '抖音号'),
    '知乎': ('知乎ID',),
    '小红书': ('小红书ID',),
    '微博': ('微博ID',),
}

HOMEPAGE_FIELDS = {
    'B站': ('B站主页链接',),
    '抖音': ('抖音主页',),
    '知乎': ('知乎主页',),
    '小红书': ('小红书主页链接',),
    '微博': ('微博主页链接',),
    '视频号': ('视频号主页链接',),
}


def text(value):
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip() or None


def fans(value):
    value = text(value)
    if value is None:
        return None
    match = re.fullmatch(r'(\d+(?:\.\d+)?)\s*([wW万亿]?)', value.replace(',', ''))
    if not match:
        raise ValueError(f'无法解析粉丝量：{value}')
    multiplier = {'': 1, 'w': 10000, 'W': 10000, '万': 10000, '亿': 100000000}[match[2]]
    count = Decimal(match[1]) * multiplier
    if count != count.to_integral_value():
        raise ValueError('粉丝量转换后不是整数')
    return int(count)


def extract(source):
    import openpyxl
    workbook = openpyxl.load_workbook(source, read_only=True, data_only=True)
    result = []
    try:
        values = iter(workbook['所有账号'].values)
        headers = next(values)
        for number, values_row in enumerate(values, 2):
            row = dict(zip(headers, values_row))
            if not any(v is not None for v in values_row):
                continue
            nickname = text(row.get('账号名称'))
            platforms = text(row.get('平台'))
            if not platforms:
                raise ValueError(f'第 {number} 行缺少平台')
            for platform in dict.fromkeys(re.split(r'[,，、]\s*', platforms)):
                platform = platform.strip()
                if not platform:
                    raise ValueError(f'第 {number} 行有空平台')
                account_id = next((text(row.get(field)) for field in ID_FIELDS.get(platform, ())
                                   if text(row.get(field))), None)
                homepage_url = next((text(row.get(field)) for field in HOMEPAGE_FIELDS.get(platform, ())
                                     if text(row.get(field))), None)
                result.append((platform, nickname, None, fans(row.get(platform + '粉丝量')), account_id,
                               homepage_url, None))
    finally:
        workbook.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', nargs='?')
    parser.add_argument('--prepared', help='已按目标六列提取的 JSON 文件')
    parser.add_argument('--extract-to', help='仅提取，不写数据库')
    args = parser.parse_args()
    if args.prepared:
        with open(args.prepared, encoding='utf-8') as handle:
            rows = json.load(handle)
    elif args.source:
        rows = extract(args.source)
    else:
        parser.error('必须提供工作簿或 --prepared')
    if args.extract_to:
        with open(args.extract_to, 'w', encoding='utf-8') as handle:
            json.dump(rows, handle, ensure_ascii=False)
    else:
        from db import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(DDL)
                cur.execute('SELECT COUNT(*) AS n FROM `平台运营账号`')
                if cur.fetchone()['n']:
                    raise ValueError('目标表已有数据，停止导入，避免重复或覆盖')
                cur.executemany('INSERT INTO `平台运营账号` (`平台`,`昵称`,`cookie`,`粉丝`,`ID`,`主页链接`,`Bit窗口`) '
                                'VALUES (%s,%s,%s,%s,%s,%s,%s)', rows)
            conn.commit()
    print(json.dumps({'records': len(rows), 'platforms': dict(Counter(r[0] for r in rows)),
                      'missing_id': sum(r[4] is None for r in rows),
                      'missing_homepage_url': sum(r[5] is None for r in rows),
                      'missing_fans': sum(r[3] is None for r in rows)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
