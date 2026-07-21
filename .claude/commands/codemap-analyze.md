---
description: Run full code analysis — dead code, duplicates, circular deps — and get recommendations
---

Run a comprehensive code analysis and produce actionable recommendations.

Steps:
1. Call `codemap_analyze` to get dead code, duplicate functions, and circular dependencies
2. Call `codemap_health` to get the current health score and hotspots
3. If a scope was specified ($ARGUMENTS), call `codemap_analyze` with scope set to that module/area

Present results as a prioritized action list:
- **Duplicates to consolidate**: functions that do the same thing in different files — merge them
- **Dead code to delete**: unreachable functions safe to remove immediately
- **Circular deps to break**: import cycles with minimum-cut suggestions
- **Complexity hotspots**: functions to simplify or split
- **Overall health**: score, trend, and top improvement opportunities
