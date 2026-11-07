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
echo "1. 正在同步更新（同时拉取 GitHub 网页 Sync 后的 origin/main 与官方 upstream）..."
echo "============================================================"
git fetch origin main || true
git fetch upstream main || true

current_branch=$(git rev-parse --abbrev-ref HEAD)
echo ""
echo "当前工作分支: $current_branch"
echo "最新上游提交:"
git log --oneline -n 3 origin/main 2>/dev/null || git log --oneline -n 3 upstream/main
echo ""

echo "============================================================"
echo "2. 正在将最新的原版代码合并到二开分支 $current_branch..."
echo "============================================================"
# 优先使用 origin/main（您在 GitHub 网页 Sync 后的分支）；若无则使用 upstream/main
if git rev-parse --verify origin/main >/dev/null 2>&1; then
    git merge origin/main
else
    git merge upstream/main
fi

echo ""
echo "============================================================"
echo "同步合并完成！所有自研二开代码已安全保留在 $current_branch 中。"
echo "============================================================"
