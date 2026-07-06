import json
from types import SimpleNamespace

from services.mcp_knowledge_gateway import MCPKnowledgeGateway


def text_block(text):
    return SimpleNamespace(text=text)


def test_gateway_parses_nested_references_json_from_modular_rag():
    references = {
        "citations": [
            {
                "index": 1,
                "chunk_id": "chunk-a",
                "source": "/tmp/hair_coloring_care.pdf",
                "score": 0.42,
                "text_snippet": "染后48小时内减少高温清洗。",
                "metadata": {
                    "title": "染发护理",
                    "chunk_index": 2,
                    "doc_type": "pdf",
                },
            }
        ],
        "metadata": {
            "query": "染发后怎么护理？",
            "result_count": 1,
            "collection": "salon_knowledge",
        },
        "has_images": False,
        "image_count": 0,
    }
    result = SimpleNamespace(content=[
        text_block("## 检索结果\n染后48小时内减少高温清洗。"),
        text_block("\n---\n**References (JSON):**\n```json\n" + json.dumps(references, ensure_ascii=False, indent=2) + "\n```"),
    ])
    gateway = MCPKnowledgeGateway(True, "python", "module", "/tmp", "salon_knowledge", 4)

    parsed = gateway._parse_call_tool_result(result)

    assert parsed.collection == "salon_knowledge"
    assert parsed.sources[0].title == "染发护理"
    assert parsed.sources[0].source.endswith("hair_coloring_care.pdf")
    assert parsed.sources[0].text_snippet == "染后48小时内减少高温清洗。"


def test_gateway_excludes_references_json_from_answer_content():
    result = SimpleNamespace(content=[
        text_block("## 检索结果\n门店营业时间为10:00-22:00。"),
        text_block('\n---\n**References (JSON):**\n```json\n{"citations":[],"metadata":{}}\n```'),
    ])
    gateway = MCPKnowledgeGateway(True, "python", "module", "/tmp", "salon_knowledge", 4)

    parsed = gateway._parse_call_tool_result(result)

    assert "References (JSON)" not in parsed.content
    assert "10:00-22:00" in parsed.content
