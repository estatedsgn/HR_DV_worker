__all__ = ["LangGraphFunnelGateway"]


def __getattr__(name: str):
    if name == "LangGraphFunnelGateway":
        from app.services.funnel_graph.gateway import LangGraphFunnelGateway

        return LangGraphFunnelGateway
    raise AttributeError(name)
