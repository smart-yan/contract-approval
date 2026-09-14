"""Graph 的边。条件边的判断函数是纯函数，单独成文件以便测试走向。"""

from app.graph.edges.routing import RouteBranch, route_after_validate

__all__ = ["RouteBranch", "route_after_validate"]
