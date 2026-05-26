from __future__ import annotations

from nexcraft_admin.models import OntologyFoundryConfig
from ontology_foundry.extractors import HuggingFaceNerExtractor, RuleBasedNerExtractor
from ontology_foundry.pipeline import OntologyFoundryPipeline
from ontology_foundry.retrieval import KeywordRetrievalAgent, LlmRetrievalAgent


def build_foundry_pipeline(config: OntologyFoundryConfig) -> OntologyFoundryPipeline:
    extractors = []
    retrieval_agents = []

    for extractor_config in config.extractors:
        if not extractor_config.enabled:
            continue
        if extractor_config.kind == "rule_based_ner":
            extractors.append(RuleBasedNerExtractor())
        elif extractor_config.kind == "huggingface_ner":
            model_name = extractor_config.options.get("model_name", "dslim/bert-base-NER")
            extractors.append(HuggingFaceNerExtractor(model_name=model_name))

    for retrieval_config in config.retrieval_agents:
        if not retrieval_config.enabled:
            continue
        if retrieval_config.kind == "keyword":
            retrieval_agents.append(KeywordRetrievalAgent())
        elif retrieval_config.kind == "llm":
            provider = retrieval_config.options.get("provider", "openai")
            model = retrieval_config.options.get("model", "gpt-4o-mini")
            retrieval_agents.append(LlmRetrievalAgent(provider=provider, model=model))

    return OntologyFoundryPipeline(extractors=extractors, retrieval_agents=retrieval_agents)
