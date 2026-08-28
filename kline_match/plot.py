"""查询窗口与 TOP3 历史窗口的 z-score 收盘价叠加图。"""

from __future__ import annotations

from pathlib import Path

import numpy as np

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


def write_overlay_png(
    series: dict[str, np.ndarray],
    path: Path,
    title: str,
) -> None:
    """``series`` 必须含 ``query``，其余键为对比曲线。最多画 query + 3 条匹配。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    query = series.get("query")
    others = [(k, v) for k, v in series.items() if k != "query"][:3]
    n_panels = max(1, len(others))
    fig, axes = plt.subplots(n_panels, 1, figsize=(10, 2.8 * n_panels), sharex=True)
    if n_panels == 1:
        axes = [axes]
    x_q = np.arange(query.size) if query is not None else np.arange(0)
    if not others:
        ax = axes[0]
        if query is not None:
            ax.plot(x_q, query, color="#2563eb", lw=2.0, label="query")
        ax.set_title(title + " (no matches)")
        ax.grid(True, alpha=0.3)
        ax.legend()
    else:
        for ax, (name, ys) in zip(axes, others):
            if query is not None:
                ax.plot(x_q, query, color="#2563eb", lw=2.2, label="query", zorder=3)
            x = np.arange(len(ys))
            ax.plot(x, ys, color="#ea580c", lw=1.6, label=name, alpha=0.9)
            ax.set_ylabel("z-score close")
            ax.grid(True, alpha=0.3)
            ax.legend(loc="upper left", fontsize=8)
            ax.set_title(name)
        axes[-1].set_xlabel("bar index (aligned)")
        fig.suptitle(title, fontsize=12)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
