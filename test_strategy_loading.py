#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试策略加载功能
"""

import sys
import os

# 添加项目路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'packs', 'Qbot'))

# 从strategy_loader导入策略加载函数
try:
    from qbot.strategies.strategy_loader import (
        load_strategies_from_directory,
        init_strategy_map,
        STRATEGY_MAP,
        STRATEGIES_DIR
    )
    
    print("=" * 60)
    print("测试策略加载功能")
    print("=" * 60)
    print(f"策略目录: {STRATEGIES_DIR}")
    print(f"目录存在: {os.path.exists(STRATEGIES_DIR)}")
    
    # 初始化策略映射
    print("\n正在加载策略...")
    strategies = init_strategy_map()
    
    print(f"\n共加载 {len(strategies)} 个策略:")
    print("-" * 60)
    for name in sorted(strategies.keys()):
        print(f"  ✓ {name}")
    print("=" * 60)
    
except Exception as e:
    print(f"\n测试失败: {e}")
    import traceback
    print(f"\n错误详情:\n{traceback.format_exc()}")
