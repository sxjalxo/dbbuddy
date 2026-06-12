# ── Explainability UI Presentation Layer ───────────────────────────────────
"""
Clean presentation layer for query explainability.

Presents:
- Intent explanation
- Semantic mapping
- Join reasoning
- Confidence scoring
- Query type detection
"""

from typing import Dict, Optional


class ExplainabilityPresenter:
    """Present query explainability in a clean, user-friendly format."""
    
    @staticmethod
    def present_query_result(result: Dict) -> Dict:
        """Present query result with explainability in clean format."""
        return {
            "query": result.get("query", ""),
            "understanding": ExplainabilityPresenter._present_understanding(result),
            "reasoning": ExplainabilityPresenter._present_reasoning(result),
            "execution": ExplainabilityPresenter._present_execution(result),
            "safety": ExplainabilityPresenter._present_safety(result)
        }
    
    @staticmethod
    def _present_understanding(result: Dict) -> Dict:
        """Present query understanding (intent, semantic mapping)."""
        intent_explanation = result.get("intent_explanation", {})
        semantic_interpretation = result.get("semantic_interpretation", {})
        query_type = result.get("query_type", "unknown")
        
        return {
            "intent": {
                "primary_action": intent_explanation.get("primary_action", "unknown"),
                "target_tables": intent_explanation.get("target_tables", []),
                "operations": intent_explanation.get("operations", [])
            },
            "semantic_mapping": semantic_interpretation,
            "query_type": query_type,
            "complexity": ExplainabilityPresenter._assess_complexity(result)
        }
    
    @staticmethod
    def _present_reasoning(result: Dict) -> Dict:
        """Present query reasoning (joins, relationships)."""
        join_reasoning = result.get("join_reasoning", [])
        model_used = result.get("model_used", "unknown")
        
        return {
            "join_reasoning": join_reasoning,
            "relationships_inferred": len(join_reasoning),
            "model_used": model_used,
            "ai_provider": model_used
        }
    
    @staticmethod
    def _present_execution(result: Dict) -> Dict:
        """Present execution information."""
        confidence = result.get("confidence", "unknown")
        confidence_reasoning = result.get("confidence_reasoning", [])
        auto_executed = result.get("auto_executed", False)
        results = result.get("results", None)
        error = result.get("error", None)
        
        return {
            "confidence": {
                "level": confidence,
                "reasoning": confidence_reasoning if confidence != "high" else []
            },
            "execution": {
                "auto_executed": auto_executed,
                "has_results": results is not None,
                "result_count": len(results) if results else 0,
                "error": error
            }
        }
    
    @staticmethod
    def _present_safety(result: Dict) -> Dict:
        """Present safety information."""
        requires_confirmation = result.get("requires_confirmation", False)
        safety_category = result.get("safety_category", "unknown")
        warning = result.get("warning", None)
        dry_run = result.get("dry_run", None)
        
        safety_info = {
            "category": safety_category,
            "requires_confirmation": requires_confirmation
        }
        
        if warning:
            safety_info["warning"] = warning
        
        if dry_run:
            safety_info["dry_run"] = {
                "estimated_rows": dry_run.get("estimated_rows", 0),
                "count_query": dry_run.get("count_query", "")
            }
        
        return safety_info
    
    @staticmethod
    def _assess_complexity(result: Dict) -> str:
        """Assess query complexity based on various factors."""
        join_count = len(result.get("join_reasoning", []))
        query_type = result.get("query_type", "unknown")
        confidence = result.get("confidence", "high")
        
        if join_count == 0 and query_type == "select":
            return "simple"
        elif join_count <= 2 and confidence == "high":
            return "moderate"
        elif join_count > 2 or confidence in ["medium", "low"]:
            return "complex"
        else:
            return "unknown"
    
    @staticmethod
    def present_for_ui(result: Dict) -> Dict:
        """Present result specifically for UI consumption."""
        return {
            "🧠 Understanding": {
                "Intent": result.get("intent_explanation", {}).get("primary_action", "unknown"),
                "Query Type": result.get("query_type", "unknown").upper(),
                "Complexity": ExplainabilityPresenter._assess_complexity(result).title()
            },
            "🔗 Relationships": {
                "Joins Inferred": len(result.get("join_reasoning", [])),
                "Details": result.get("join_reasoning", [])
            },
            "⚡ Model": {
                "Provider": result.get("model_used", "unknown"),
                "Confidence": result.get("confidence", "unknown").upper()
            },
            "📊 Execution": {
                "Auto-executed": result.get("auto_executed", False),
                "Has Results": result.get("results") is not None,
                "Error": result.get("error", None)
            },
            "🛡️ Safety": {
                "Category": result.get("safety_category", "unknown").upper(),
                "Requires Confirmation": result.get("requires_confirmation", False),
                "Warning": result.get("warning", None)
            }
        }
    
    @staticmethod
    def present_compact(result: Dict) -> str:
        """Present result in compact text format."""
        lines = []
        
        # Query
        lines.append(f"Query: {result.get('query', '')}")
        
        # Understanding
        intent = result.get("intent_explanation", {}).get("primary_action", "unknown")
        lines.append(f"Intent: {intent}")
        
        # SQL
        lines.append(f"SQL: {result.get('sql', '')}")
        
        # Confidence
        confidence = result.get("confidence", "unknown")
        lines.append(f"Confidence: {confidence}")
        
        # Execution
        auto_executed = result.get("auto_executed", False)
        lines.append(f"Auto-executed: {auto_executed}")
        
        # Safety
        requires_confirmation = result.get("requires_confirmation", False)
        if requires_confirmation:
            lines.append(f"⚠️ Requires confirmation")
            warning = result.get("warning", "")
            if warning:
                lines.append(f"Warning: {warning}")
        
        return "\n".join(lines)


def present_query_result(result: Dict, format: str = "structured") -> Dict | str:
    """Present query result in specified format.
    
    Args:
        result: Query result dictionary
        format: "structured", "ui", or "compact"
    
    Returns:
        Formatted result
    """
    if format == "ui":
        return ExplainabilityPresenter.present_for_ui(result)
    elif format == "compact":
        return ExplainabilityPresenter.present_compact(result)
    else:
        return ExplainabilityPresenter.present_query_result(result)
