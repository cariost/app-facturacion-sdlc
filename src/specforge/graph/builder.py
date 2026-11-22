"""Construcción del `StateGraph` de SpecForge.

    START ─route─▶ intake_po ─route─▶ story_refinement ─route─▶ blueprint_design
                      ▲  │                 ▲  │ (rondas)              │
                      └clarification◀──────┘  │                       ▼
                                                            human_gate (interrupt)
                                     qa_verification ◀── implementation ◀─┘
                                        │  └──(QA falla, reintento)──▶ implementation
                                        ▼
                                       END  (COMPLETED / FAILED / BLOCKED / cambio de hilo)

Todas las aristas pasan por `GraphNodes.route`, que decide por la fase persistida.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from langgraph.graph import START, StateGraph

from specforge.graph.nodes import PHASE_NODES, ROUTES, GraphNodes
from specforge.graph.state import GraphState

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.graph.state import CompiledStateGraph

    NodeFn = Callable[[GraphState], Awaitable[dict[str, Any]]]


class StateNode(Protocol):
    def __call__(self, state: GraphState) -> Awaitable[dict[str, Any]]: ...


def _node(fn: NodeFn) -> StateNode:
    """Adapta la firma al protocolo de nodos de LangGraph (parámetro `state`)."""

    async def node(state: GraphState) -> dict[str, Any]:
        return await fn(state)

    node.__name__ = getattr(fn, "__name__", "node")
    return node


def build_graph(nodes: GraphNodes) -> StateGraph[GraphState]:
    graph: StateGraph[GraphState] = StateGraph(GraphState)
    for phase, name in PHASE_NODES.items():
        if name == "human_gate":
            graph.add_node(name, _node(nodes.human_gate))
        else:
            graph.add_node(name, _node(nodes.agent_node(phase)))
    graph.add_node("clarification", _node(nodes.clarification))
    graph.add_conditional_edges(START, nodes.route, ROUTES)
    for name in [*PHASE_NODES.values(), "clarification"]:
        graph.add_conditional_edges(name, nodes.route, ROUTES)
    return graph


def compile_graph(
    nodes: GraphNodes, checkpointer: BaseCheckpointSaver[str]
) -> CompiledStateGraph[GraphState]:
    return build_graph(nodes).compile(checkpointer=checkpointer)
