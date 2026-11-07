#!/usr/bin/env bash
set -euo pipefail

# 自动同步官方 upstream 最新代码的辅助工具
# 默认合并到当前的 custom-main 二开分支，全程不会执行 git push

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if ! git remote | grep -q "^upstream$"; then
    echo "未检测到 upstream 源，正在添加官方仓库..."
    git remote add upstream https://github.com/myfanhua/turb-gpt-free-register.git
fi

echo "============================================================"
echo "1. 正在获取官方上游最新代码（git fetch upstream）..."
echo "============================================================"
git fetch upstream

current_branch=$(git rev-parse --abbrev-ref HEAD)
echo ""
echo "当前工作分支: $current_branch"
echo "官方最新提交:"
git log --oneline -n 3 upstream/main
echo ""

echo "============================================================"
echo "2. 正在将 upstream/main 合并到当前分支 $current_branch..."
echo "============================================================"
git merge upstream/main

echo ""
echo "============================================================"
echo "同步合并完成！所有自研二开代码已安全保留在本地分支中。"
echo "============================================================"
