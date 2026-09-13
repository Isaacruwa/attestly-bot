"""
Keyword-triggered knowledge base for the Attestly bot.
No LLM involved: each topic has a list of trigger phrases and a fixed
answer. The matcher (in bot.py) scans incoming messages for these phrases
and replies with the best-matching topic's answer.

To add a topic: add an entry to TOPICS with keywords (lowercase) and an
answer. Order matters only for tie-breaking (first match wins on ties).
"""

TOPICS = [
    {
        "id": "what_is_attestly",
        "keywords": ["what is attestly", "what does attestly do", "what is this bot",
                     "who are you", "what is this tool"],
        "answer": (
            "Attestly turns your AI agents' operational traces (tool calls, model calls, "
            "human interventions, errors) into audit-ready EU AI Act Annex IV technical "
            "documentation, continuously \u2014 not as a quarterly scramble. "
            "See www.attestly.online for details."
        ),
    },
    {
        "id": "pricing",
        "keywords": ["price", "pricing", "cost", "how much", "free plan", "free tier", "subscription"],
        "answer": (
            "There's a free tier: 1 AI system and 10 lifetime documentation generations, "
            "enough to draft one system's full Annex IV documentation. "
            "Full pricing: www.attestly.online/pricing"
        ),
    },
    {
        "id": "annex_iv",
        "keywords": ["annex iv", "technical documentation", "what documents", "what does it generate"],
        "answer": (
            "Attestly drafts four things from your traces: Annex IV technical documentation, "
            "risk-management summaries, conformity-assessment checklists, and audit-ready "
            "evidence trails (every sentence links back to the trace event that justified it)."
        ),
    },
    {
        "id": "trace_sources",
        "keywords": ["opentelemetry", "langsmith", "agentops", "mcp logs", "what traces",
                     "what format", "trace source", "supported traces"],
        "answer": (
            "Attestly ingests OpenTelemetry traces, LangSmith runs, AgentOps sessions, MCP logs, "
            "and generic pre-normalized JSON \u2014 so any framework that can export trace events "
            "as JSON can be used, even without native support."
        ),
    },
    {
        "id": "risk_tiers",
        "keywords": ["risk tier", "risk level", "risk categor", "prohibited high-risk limited minimal",
                     "what are the risk levels"],
        "answer": (
            "The EU AI Act has four risk tiers: Prohibited (banned outright, e.g. social scoring, "
            "subliminal manipulation), High-risk (Annex III areas like biometrics, employment, "
            "law enforcement \u2014 strict requirements), Limited-risk (transparency obligations, "
            "e.g. chatbots must disclose they're AI), and Minimal-risk (few specific obligations). "
            "Run /riskcheck in this bot for a directional read on your own system."
        ),
    },
    {
        "id": "prohibited_practices",
        "keywords": ["prohibited practice", "banned practice", "article 5", "social scoring",
                     "subliminal manipulation"],
        "answer": (
            "Prohibited practices (Article 5) include: subliminal manipulation, exploiting "
            "vulnerabilities, social scoring, real-time remote biometric ID by law enforcement "
            "in public spaces (with narrow exceptions), and emotion recognition in workplaces "
            "or schools. These have been banned since February 2025."
        ),
    },
    {
        "id": "high_risk_annex_iii",
        "keywords": ["annex iii", "high-risk area", "high risk system", "high-risk system"],
        "answer": (
            "Annex III high-risk areas include: biometrics, critical infrastructure, education/"
            "training, employment/worker management, access to essential services, law "
            "enforcement, migration/border control, and administration of justice or democratic "
            "processes. High-risk obligations under Annex III were deferred by the EU's Digital "
            "Omnibus to December 2, 2027."
        ),
    },
    {
        "id": "gpai",
        "keywords": ["gpai", "general-purpose ai", "general purpose model", "foundation model obligations"],
        "answer": (
            "GPAI (General-Purpose AI) models \u2014 like foundation/LLM models \u2014 have their own "
            "obligations under the Act, separate from the risk-tier system, especially models "
            "classified as having 'systemic risk'. Attestly can flag GPAI-related evidence in "
            "your documentation, but a full GPAI compliance read needs specialist review."
        ),
    },
    {
        "id": "transparency",
        "keywords": ["transparency obligation", "chatbot disclosure", "deepfake label", "ai generated content label"],
        "answer": (
            "Transparency obligations apply to things like chatbots (must disclose they're AI) "
            "and AI-generated content (must be labeled as machine-generated from December 2, 2026). "
            "The chatbot transparency deadline is August 2, 2026."
        ),
    },
    {
        "id": "fines",
        "keywords": ["fine", "penalty", "penalties", "how much is the fine"],
        "answer": (
            "Non-compliance can carry fines up to \u20ac35 million or 7% of global annual turnover, "
            "whichever is higher, for the most serious violations (prohibited practices). "
            "Other violations carry lower tiered maximums."
        ),
    },
    {
        "id": "vs_grc",
        "keywords": ["grc platform", "vs grc", "governance platform", "compliance platform difference"],
        "answer": (
            "Broad AI-governance/GRC platforms track policy, inventory, and ownership \u2014 useful "
            "for knowing a system exists. Attestly ingests actual runtime traces and drafts the "
            "Annex IV documentation itself, with evidence links back to specific events. Most "
            "teams use both: GRC for inventory, Attestly for the evidence-backed documentation."
        ),
    },
    {
        "id": "vs_attestly_dev",
        "keywords": ["attestly.dev", "attestly dev", "difference from attestly.dev"],
        "answer": (
            "attestly.dev does static code analysis for generic SaaS privacy/compliance docs. "
            "Attestly (attestly.online) reads AI agent runtime traces and drafts EU AI Act Annex IV "
            "documentation with evidence links to specific trace events \u2014 a different product."
        ),
    },
    {
        "id": "legal_advice",
        "keywords": ["is this legal advice", "legally binding", "guarantee compliance", "is it legal advice"],
        "answer": (
            "No \u2014 Attestly does not provide legal advice and does not guarantee regulatory "
            "compliance. Every generated section is reviewed, edited, and approved by a human "
            "before it counts as final. For a legal opinion, talk to counsel."
        ),
    },
    {
        "id": "who_for",
        "keywords": ["who is it for", "who is attestly for", "is this for me", "target audience"],
        "answer": (
            "Attestly is built for AI startups shipping agents to EU customers, enterprise AI "
            "teams running multiple systems, compliance/risk teams, and AI governance "
            "consultancies producing documentation for clients."
        ),
    },
    {
        "id": "contact_support",
        "keywords": ["support", "contact", "talk to a human", "help me directly"],
        "answer": (
            "You can reach the Attestly team directly at support@attestly.online, or use /riskcheck "
            "and /generate here for the free self-serve tools."
        ),
    },
]
