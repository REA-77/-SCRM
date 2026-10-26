"""核对并同步指定知乎账号的主页链接。"""
import argparse

from db import get_conn


LINKS = [
    ("家电夏洛克", "https://www.zhihu.com/people/lidongcha4.com"),
    ("taoyang", "https://www.zhihu.com/people/tao-yang-76"),
    ("李bro", "https://www.zhihu.com/people/lidongcha.com"),
    ("蓝小狗", "https://www.zhihu.com/people/lan-lan-lan-xiao-gou"),
    ("新月", "https://www.zhihu.com/people/wangstar121"),
    ("家电小钢炮", "https://www.zhihu.com/people/ukey-59"),
    ("家电人老王", "https://www.zhihu.com/people/wang-rong-27-73-34"),
    ("包先生", "https://www.zhihu.com/people/zhekek"),
    ("小菘蓝", "https://www.zhihu.com/people/yuanshansidaoying"),
    ("智家百晓生", "https://www.zhihu.com/people/wang-xin-chen-84"),
    ("剑风", "https://www.zhihu.com/people/jiajuyanjiuyuan"),
    ("南风生涯", "https://www.zhihu.com/people/fengfengfengwang"),
    ("小黄人", "https://www.zhihu.com/people/xiaohuangren95"),
    ("大碗宽面", "https://www.zhihu.com/people/ding-li-11"),
    ("懂老师", "https://www.zhihu.com/people/duo-kan-le-ji-yan"),
    ("西瓜糖", "https://www.zhihu.com/people/pi-pi-xia-you-meng-xiang"),
    ("老颜说家电", "https://www.zhihu.com/people/haoyin-zhou"),
    ("Wangjun", "https://www.zhihu.com/people/wangjun-71"),
    ("苏卿羽", "https://www.zhihu.com/people/xtpjd"),
    ("小语要努力上进", "https://www.zhihu.com/people/zong-zheng-hui"),
    ("文喵不高兴", "https://www.zhihu.com/people/wenmiaoxiong"),
    ("半毛", "https://www.zhihu.com/people/jun-bu-jian-qing-66"),
    ("易志明", "https://www.zhihu.com/people/swljd"),
    ("迪丽冷巴", "https://www.zhihu.com/people/lidongcha10"),
    ("行者K叔", "https://www.zhihu.com/people/huang-lun-hua-39"),
    ("托马斯学长", "https://www.zhihu.com/people/wanxi_zhihu"),
    ("扎维耶小丑", "https://www.zhihu.com/people/lishichouge"),
    ("观察君", "https://www.zhihu.com/people/zhongnihei"),
    ("阿宅家电", "https://www.zhihu.com/people/yangyangicon"),
]


def main(commit: bool) -> None:
    names = [name for name, _ in LINKS]
    placeholders = ",".join(["%s"] * len(names))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT `昵称` AS nickname, COALESCE(`主页链接`, '') AS homepage_url "
                "FROM `平台运营账号` WHERE `平台`='知乎' "
                f"AND `昵称` IN ({placeholders})",
                names,
            )
            existing = {row["nickname"]: row["homepage_url"].strip() for row in cur.fetchall()}
            missing = [name for name in names if name not in existing]
            to_update = [(url, name) for name, url in LINKS if existing.get(name) != url]
            if commit:
                for url, name in to_update:
                    cur.execute(
                        "UPDATE `平台运营账号` SET `主页链接`=%s "
                        "WHERE `平台`='知乎' AND `昵称`=%s",
                        (url, name),
                    )
                conn.commit()
    print({
        "requested": len(LINKS),
        "matched": len(existing),
        "updated_or_would_update": len(to_update),
        "already_current": len(existing) - len(to_update),
        "unmatched": missing,
        "committed": commit,
    })


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true", help="写入数据库")
    main(parser.parse_args().commit)
