# ECC (affaan-m/ECC) – übernommene Auswahl

Quelle: https://github.com/affaan-m/ECC, Commit `25278e7d7b6cf6e6a4e7a6c8e10f14ff22f0eb5b` (geprüft am 2026-10-28), Lizenz MIT (siehe `ECC-LICENSE`).

Übernommen wurde nur Text (Anleitungen für Claude Code), **keine Hooks, keine Skripte, kein Installer**:

| Art | Inhalt | Wozu in dieser Firma |
|---|---|---|
| Agents (`.claude/agents/`) | security-reviewer, silent-failure-hunter, python-reviewer, fastapi-reviewer, tdd-guide, code-reviewer, planner | Reviews/Planung beim Weiterentwickeln mit Claude Code |
| Skills (`.claude/skills/`) | llm-trading-agent-security, agent-security-hardening, tdd-workflow, python-testing, verification-loop, security-review, mcp-dependency-review, eval-harness | Checklisten; die Trading-Security- und Eval-Teile sind zusätzlich im Code umgesetzt (siehe docs/AUDIT_2026-10.md, Runde 4) |
| Rules (`.claude/rules/ecc/`) | common: coding-style, security, testing, code-review, patterns · python: coding-style, fastapi, patterns, security, testing | Coding-Standards |

Updates nur bewusst: neuen Commit prüfen, Dateien vergleichen, SHA hier anpassen. Die Projektregeln in `CLAUDE.md`
(Geld-Sicherheit, Gates, keine Gewinnversprechen) haben Vorrang vor allem aus ECC.
