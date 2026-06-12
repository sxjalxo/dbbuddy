# ── Demo Benchmark Dataset ─────────────────────────────────────────────────
"""
Benchmark dataset with demo queries and expected outputs for presentation.

This provides structured benchmarking with presentation-ready output formatting.
"""

from demo_queries import ALL_DEMO_QUERIES, DEMO_FLOW
from dbbuddy_core.pipeline import benchmark_query
from dbbuddy_core.models import DBConfig


def format_benchmark_result(query_obj, results):
    """Format benchmark results for presentation."""
    query = query_obj["query"]
    expected_type = query_obj["expected_type"]
    
    output = f"Query: {query}\n"
    output += f"Expected Type: {expected_type}\n"
    output += f"Complexity: {query_obj.get('complexity', 'unknown')}\n\n"
    
    for provider, result in results["results"].items():
        output += f"{provider.capitalize()}:\n"
        output += f"  - SQL: {result.get('sql', 'N/A')}\n"
        output += f"  - Confidence: {result.get('confidence', 'N/A')}\n"
        output += f"  - Time: {result.get('latency_ms', 'N/A')}ms\n"
        output += f"  - Execution: {result.get('execution_correctness', 'N/A')}\n"
        output += "\n"
    
    output += f"Result: {results['summary'].get('accuracy_percentage', 0)}% accuracy\n"
    output += f"Best Provider: {results['summary'].get('best_provider', 'N/A')}\n"
    
    return output


def run_demo_benchmark(config, providers=["local", "nemotron"]):
    """Run benchmark on all demo queries."""
    results = []
    
    for query_obj in ALL_DEMO_QUERIES:
        print(f"\n{'='*60}")
        print(f"Query: {query_obj['query']}")
        print(f"Tier: {query_obj['tier']}")
        print(f"Expected: {query_obj['expected_type']}")
        print(f"{'='*60}\n")
        
        try:
            result = benchmark_query(config, query_obj["query"], providers)
            
            formatted = format_benchmark_result(query_obj, result)
            print(formatted)
            
            results.append({
                "query": query_obj,
                "result": result,
                "formatted": formatted
            })
        except Exception as e:
            print(f"Error: {e}")
            results.append({
                "query": query_obj,
                "result": None,
                "error": str(e)
            })
    
    return results


def run_demo_flow(config, providers=["local"]):
    """Run the 6-step demo flow for presentation."""
    flow_results = []
    
    for step in DEMO_FLOW:
        print(f"\n{'='*60}")
        print(f"Step {step['step']}: {step['title']}")
        print(f"Query: {step['query']}")
        print(f"Purpose: {step['purpose']}")
        print(f"{'='*60}\n")
        
        try:
            result = benchmark_query(config, step["query"], providers)
            
            # Format for presentation
            output = f"Query: {step['query']}\n\n"
            
            for provider, provider_result in result["results"].items():
                output += f"{provider.capitalize()}:\n"
                output += f"  - SQL: {provider_result.get('sql', 'N/A')}\n"
                output += f"  - Confidence: {provider_result.get('confidence', 'N/A')}\n"
                output += f"  - Time: {provider_result.get('latency_ms', 'N/A')}ms\n"
                output += "\n"
            
            print(output)
            
            flow_results.append({
                "step": step,
                "result": result,
                "formatted": output
            })
        except Exception as e:
            print(f"Error: {e}")
            flow_results.append({
                "step": step,
                "result": None,
                "error": str(e)
            })
    
    return flow_results


def get_benchmark_summary(results):
    """Generate summary statistics from benchmark results."""
    total = len(results)
    successful = sum(1 for r in results if r["result"] is not None)
    failed = total - successful
    
    # Calculate average latency
    latencies = []
    for r in results:
        if r["result"]:
            for provider, result in r["result"]["results"].items():
                if "latency_ms" in result:
                    latencies.append(result["latency_ms"])
    
    avg_latency = sum(latencies) / len(latencies) if latencies else 0
    
    summary = {
        "total_queries": total,
        "successful": successful,
        "failed": failed,
        "success_rate": (successful / total * 100) if total > 0 else 0,
        "average_latency_ms": avg_latency,
        "total_latency_ms": sum(latencies)
    }
    
    return summary


if __name__ == "__main__":
    # Example usage
    config = DBConfig(
        host="localhost",
        user="test",
        password="test",
        database="testdb",
        ai=True,
        ai_provider="local"
    )
    
    print("Running Demo Benchmark...")
    results = run_demo_benchmark(config)
    
    print("\n" + "="*60)
    print("BENCHMARK SUMMARY")
    print("="*60)
    summary = get_benchmark_summary(results)
    print(f"Total Queries: {summary['total_queries']}")
    print(f"Successful: {summary['successful']}")
    print(f"Failed: {summary['failed']}")
    print(f"Success Rate: {summary['success_rate']:.1f}%")
    print(f"Average Latency: {summary['average_latency_ms']:.1f}ms")
