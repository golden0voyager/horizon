"""Smoke tests for ``src/ai/prompts.py`` — prompt template constants.

The source file only defines module-level prompt strings; the only way to cover
them is to import them. These tests verify the contracts each prompt template
is expected to satisfy (placeholders, role definitions, JSON schema hints).
"""

from src.ai.prompts import (
    CONCEPT_EXTRACTION_SYSTEM,
    CONCEPT_EXTRACTION_USER,
    CONTENT_ANALYSIS_SYSTEM,
    CONTENT_ANALYSIS_USER,
    CONTENT_ENRICHMENT_SYSTEM,
    CONTENT_ENRICHMENT_USER,
    TOPIC_DEDUP_SYSTEM,
    TOPIC_DEDUP_USER,
)


def test_topic_dedup_prompts_exist_and_have_expected_placeholders():
    assert "deduplication assistant" in TOPIC_DEDUP_SYSTEM
    assert TOPIC_DEDUP_USER.count("{items}") == 1
    assert '"duplicates"' in TOPIC_DEDUP_USER
    # Must mention returning valid JSON.
    assert "JSON" in TOPIC_DEDUP_USER


def test_content_analysis_prompts_include_required_fields():
    assert "0-10" in CONTENT_ANALYSIS_SYSTEM or "Score" in CONTENT_ANALYSIS_SYSTEM
    for placeholder in ("{title}", "{source}", "{author}", "{url}"):
        assert placeholder in CONTENT_ANALYSIS_USER, placeholder
    assert '"score"' in CONTENT_ANALYSIS_USER
    assert '"reason"' in CONTENT_ANALYSIS_USER
    assert '"summary"' in CONTENT_ANALYSIS_USER
    assert '"tags"' in CONTENT_ANALYSIS_USER


def test_concept_extraction_prompts_target_low_level_concepts():
    assert "concept" in CONCEPT_EXTRACTION_SYSTEM.lower()
    assert CONCEPT_EXTRACTION_USER.count("{title}") == 1
    assert CONCEPT_EXTRACTION_USER.count("{summary}") == 1
    assert '"queries"' in CONCEPT_EXTRACTION_USER


def test_content_enrichment_prompts_force_bilingual_output():
    # System prompt must require English and Chinese versions of each field.
    assert "_en" in CONTENT_ENRICHMENT_SYSTEM
    assert "_zh" in CONTENT_ENRICHMENT_SYSTEM
    for placeholder in ("{title}", "{url}", "{summary}", "{score}", "{reason}", "{tags}"):
        assert placeholder in CONTENT_ENRICHMENT_USER, placeholder
    # All bilingual keys appear in the user template as expected output fields.
    expected_keys = (
        "title_en", "title_zh",
        "whats_new_en", "whats_new_zh",
        "why_it_matters_en", "why_it_matters_zh",
        "key_details_en", "key_details_zh",
        "background_en", "background_zh",
        "community_discussion_en", "community_discussion_zh",
        "sources",
    )
    for key in expected_keys:
        assert f'"{key}"' in CONTENT_ENRICHMENT_USER, key
