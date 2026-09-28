#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""铁路信号联锁校验工具（纯 Python 标准库，单文件）。

输入为行式文本（'#' 起为注释，空行忽略），定义须先于操作：

    道岔     <名称> <定位|反位>                          # 道岔定义（初始位置）
    信号机   <名称>                                      # 信号机定义
    进路     <名称> 区段=G1,G2 道岔=P1:定位,P2:反位 信号机=X1
    排列     <进路名>                                    # 操作：排列进路
    取消     <进路名>                                    # 操作：取消进路
    转换道岔 <道岔名> <定位|反位>                        # 操作：转换道岔

联锁规则：
  * 排列时所需道岔未转到位 -> 报错（进路、道岔、要求/当前位置）
  * 与已排列进路共享轨道区段（敌对进路）-> 报错（两进路、共享区段）
  * 排列成功 -> 信号机开放、区段锁闭；取消 -> 信号机关闭、区段释放
  * 转换道岔后，已排列且需该道岔原位置的进路级联失效（自动取消）
  * 引用不存在的进路/道岔/信号机、重复排列、取消未排列进路 -> 报错

用法：
    python3 interlock.py 输入文件          # 文本报告
    python3 interlock.py 输入文件 --json   # JSON 报告（便于程序校验）
    python3 interlock.py -                 # 从标准输入读取
存在错误时退出码为 1，否则为 0。
"""

import argparse
import json
import sys
from dataclasses import dataclass, field

POSITIONS = ("定位", "反位")


@dataclass
class Turnout:
    name: str
    position: str


@dataclass
class Signal:
    name: str
    state: str = "关闭"


@dataclass
class Route:
    name: str
    sections: list = field(default_factory=list)
    turnouts: dict = field(default_factory=dict)  # 道岔名 -> 要求位置
    signal: str = ""


class Interlocking:
    """联锁核心：保存设备状态，执行操作并记录事件与错误。"""

    def __init__(self):
        self.turnouts = {}
        self.signals = {}
        self.routes = {}
        self.arranged = []      # 已排列进路名（按排列顺序）
        self.events = []        # (位置, 描述) 正常事件
        self.errors = []        # (位置, 描述) 错误清单

    # ---------- 定义 ----------

    def define_turnout(self, name, position):
        if name in self.turnouts:
            self.errors.append(("定义", "道岔 '%s' 重复定义" % name))
            return
        self.turnouts[name] = Turnout(name, position)

    def define_signal(self, name):
        if name in self.signals:
            self.errors.append(("定义", "信号机 '%s' 重复定义" % name))
            return
        self.signals[name] = Signal(name)

    def define_route(self, name, sections, turnouts, signal):
        if name in self.routes:
            self.errors.append(("定义", "进路 '%s' 重复定义" % name))
            return
        for t in turnouts:
            if t not in self.turnouts:
                self.errors.append(("定义", "进路 '%s' 引用了不存在的道岔 '%s'" % (name, t)))
        if signal not in self.signals:
            self.errors.append(("定义", "进路 '%s' 引用了不存在的信号机 '%s'" % (name, signal)))
        self.routes[name] = Route(name, sections, turnouts, signal)

    # ---------- 操作 ----------

    def op_arrange(self, op_no, name):
        where = "操作 %d" % op_no
        route = self.routes.get(name)
        if route is None:
            self.errors.append((where, "排列失败：进路 '%s' 不存在" % name))
            return
        if name in self.arranged:
            self.errors.append((where, "排列失败：进路 '%s' 已排列，禁止重复排列" % name))
            return

        ok = True
        for t_name, required in route.turnouts.items():
            turnout = self.turnouts.get(t_name)
            if turnout is None:
                self.errors.append((where, "排列失败：进路 '%s' 所需道岔 '%s' 不存在" % (name, t_name)))
                ok = False
            elif turnout.position != required:
                self.errors.append((where, "排列失败：进路 '%s' 所需道岔 '%s' 未转到位（要求 %s，当前 %s）"
                                    % (name, t_name, required, turnout.position)))
                ok = False
        if route.signal not in self.signals:
            self.errors.append((where, "排列失败：进路 '%s' 的信号机 '%s' 不存在" % (name, route.signal)))
            ok = False
        for other_name in self.arranged:
            other = self.routes[other_name]
            shared = [s for s in route.sections if s in other.sections]
            if shared:
                self.errors.append((where, "排列失败：进路 '%s' 与已排列进路 '%s' 敌对（共享区段：%s）"
                                    % (name, other_name, "、".join(shared))))
                ok = False
        if not ok:
            return

        self.arranged.append(name)
        self.signals[route.signal].state = "开放"
        self.events.append((where, "进路 '%s' 排列成功：信号机 '%s' 开放，区段 %s 锁闭"
                            % (name, route.signal, "、".join(route.sections) or "（无）")))

    def op_cancel(self, op_no, name):
        where = "操作 %d" % op_no
        if name not in self.routes:
            self.errors.append((where, "取消失败：进路 '%s' 不存在" % name))
            return
        if name not in self.arranged:
            self.errors.append((where, "取消失败：进路 '%s' 未排列" % name))
            return
        self._release(where, name, "取消")

    def op_convert(self, op_no, name, position):
        where = "操作 %d" % op_no
        turnout = self.turnouts.get(name)
        if turnout is None:
            self.errors.append((where, "转换失败：道岔 '%s' 不存在" % name))
            return
        turnout.position = position
        self.events.append((where, "道岔 '%s' 转换为 %s" % (name, position)))
        # 级联失效：已排列且要求该道岔处于其他位置的进路自动取消
        for rname in list(self.arranged):
            route = self.routes[rname]
            if name in route.turnouts and route.turnouts[name] != position:
                self._release(where, rname, "级联失效（道岔 '%s' 已转换为 %s，该进路要求 %s）"
                              % (name, position, route.turnouts[name]))

    def _release(self, where, name, reason):
        route = self.routes[name]
        self.arranged.remove(name)
        sig = self.signals.get(route.signal)
        if sig is not None:
            sig.state = "关闭"
        self.events.append((where, "进路 '%s' %s：信号机 '%s' 关闭，区段 %s 释放"
                            % (name, reason, route.signal, "、".join(route.sections) or "（无）")))

    # ---------- 状态 ----------

    def locked_sections(self):
        locked = []
        for rname in self.arranged:
            for s in self.routes[rname].sections:
                if s not in locked:
                    locked.append(s)
        return locked

    def state_dict(self):
        return {
            "道岔位置": {n: t.position for n, t in self.turnouts.items()},
            "信号机状态": {n: s.state for n, s in self.signals.items()},
            "已排列进路": list(self.arranged),
            "锁闭区段": self.locked_sections(),
        }


# ---------- 输入解析 ----------

def parse(text):
    """解析输入文本，返回 (Interlocking, 操作列表)。操作: (类型, 目标, 位置或None)。"""
    il = Interlocking()
    ops = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        kw, where = parts[0], "第 %d 行" % lineno
        try:
            if kw == "道岔":
                name, pos = parts[1], parts[2]
                if pos not in POSITIONS:
                    raise ValueError("道岔位置须为 定位/反位，得到 '%s'" % pos)
                il.define_turnout(name, pos)
            elif kw == "信号机":
                il.define_signal(parts[1])
            elif kw == "进路":
                name = parts[1]
                kv = {}
                for tok in parts[2:]:
                    k, sep, v = tok.partition("=")
                    if not sep:
                        raise ValueError("进路参数须为 键=值，得到 '%s'" % tok)
                    kv[k] = v
                sections = [s for s in kv.get("区段", "").split(",") if s]
                tdict = {}
                for item in filter(None, kv.get("道岔", "").split(",")):
                    tn, sep, tp = item.partition(":")
                    if not sep or tp not in POSITIONS:
                        raise ValueError("道岔要求须为 名称:定位/反位，得到 '%s'" % item)
                    tdict[tn] = tp
                signal = kv.get("信号机", "")
                if not signal:
                    raise ValueError("进路缺少 信号机= 参数")
                il.define_route(name, sections, tdict, signal)
            elif kw in ("排列", "取消"):
                ops.append((kw, parts[1], None))
            elif kw == "转换道岔":
                name, pos = parts[1], parts[2]
                if pos not in POSITIONS:
                    raise ValueError("道岔位置须为 定位/反位，得到 '%s'" % pos)
                ops.append((kw, name, pos))
            else:
                raise ValueError("无法识别的指令 '%s'" % kw)
        except IndexError:
            il.errors.append((where, "格式错误（参数不足）：%s" % raw.strip()))
        except ValueError as exc:
            il.errors.append((where, "格式错误：%s" % exc))
    return il, ops


def run(il, ops):
    for op_no, (kind, target, pos) in enumerate(ops, 1):
        if kind == "排列":
            il.op_arrange(op_no, target)
        elif kind == "取消":
            il.op_cancel(op_no, target)
        elif kind == "转换道岔":
            il.op_convert(op_no, target, pos)


# ---------- 输出 ----------

def print_report(il):
    print("========== 联锁状态 ==========")
    print("道岔位置：")
    for n, t in il.turnouts.items():
        print("  %s = %s" % (n, t.position))
    print("信号机状态：")
    for n, s in il.signals.items():
        print("  %s = %s" % (n, s.state))
    print("已排列进路：%s" % ("、".join(il.arranged) if il.arranged else "无"))
    print("锁闭区段：%s" % ("、".join(il.locked_sections()) or "无"))
    print("========== 事件记录 ==========")
    if il.events:
        for i, (where, msg) in enumerate(il.events, 1):
            print("%2d. [%s] %s" % (i, where, msg))
    else:
        print("  （无事件）")
    print("========== 错误清单 ==========")
    if il.errors:
        for i, (where, msg) in enumerate(il.errors, 1):
            print("%2d. [%s] %s" % (i, where, msg))
    else:
        print("  无错误")


def print_json(il):
    report = {
        "联锁状态": il.state_dict(),
        "事件记录": [{"位置": w, "事件": m} for w, m in il.events],
        "错误清单": [{"位置": w, "错误": m} for w, m in il.errors],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


def main(argv=None):
    ap = argparse.ArgumentParser(description="铁路信号联锁校验工具")
    ap.add_argument("input", help="输入文件路径，'-' 表示标准输入")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出报告")
    args = ap.parse_args(argv)

    if args.input == "-":
        text = sys.stdin.read()
    else:
        with open(args.input, encoding="utf-8") as f:
            text = f.read()

    il, ops = parse(text)
    run(il, ops)
    if args.json:
        print_json(il)
    else:
        print_report(il)
    return 1 if il.errors else 0


if __name__ == "__main__":
    sys.exit(main())
