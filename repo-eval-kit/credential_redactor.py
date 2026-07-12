"""
Credential redaction for LLM analysis.
Removes secrets from code diffs before sending to OpenAI/external services.
Preserves code structure for analysis.
"""

import re
from typing import Optional


# Same patterns from repo_analyzer.py
SECRET_PATTERNS = [
    ("AWS Access Key",   r"\bAKIA[0-9A-Z]{16}\b"),
    ("GitHub Token",     r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
    ("GitLab Token",     r"\bglpat-[A-Za-z0-9_\-]{20,}\b"),
    ("Google API Key",   r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    ("Slack Token",      r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),
    ("Stripe Key",       r"\b[sp]k_(live|test)_[A-Za-z0-9]{16,}\b"),
    ("OpenAI Key",       r"\bsk-[A-Za-z0-9_\-]{20,}\b"),
    ("Anthropic Key",    r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b"),
    ("Private Key",      r"-----BEGIN (RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY"),
    ("JWT",              r"\beyJ[A-Za-z0-9_\-]{15,}\.eyJ[A-Za-z0-9_\-]{15,}"),
    ("Hardcoded secret", r"(?i)\b(password|passwd|secret|api[_-]?key|auth[_-]?token)\b\s*[:=]\s*['\"][^'\"\s]{8,}['\"]"),
]


def redact_secrets(text: str, redaction_marker: str = "[REDACTED]") -> tuple[str, list[tuple[str, int]]]:
    """
    Remove secrets from text while preserving code structure.

    Args:
        text: Code or diff text to redact
        redaction_marker: Placeholder for redacted values (default: [REDACTED])

    Returns:
        Tuple of (redacted_text, list_of_redactions)
        where list_of_redactions is [(secret_type, count_redacted), ...]

    Example:
        >>> code = 'token = "sk-1234567890abcdefghij"'
        >>> redacted, stats = redact_secrets(code)
        >>> redacted
        'token = "[REDACTED]"'
        >>> stats
        [('OpenAI Key', 1)]
    """
    redacted_text = text
    redaction_counts = {}

    for secret_name, pattern in SECRET_PATTERNS:
        matches = re.findall(pattern, text)
        if matches:
            count = len(matches) if isinstance(matches[0], str) else len([m for m in matches if m])
            redacted_text = re.sub(
                pattern,
                f'"{redaction_marker}"' if '"' in redacted_text[:100] else redaction_marker,
                redacted_text,
                flags=re.IGNORECASE
            )
            redaction_counts[secret_name] = count

    # Convert to list of tuples for consistent return
    redactions = [(name, count) for name, count in redaction_counts.items()]

    return redacted_text, redactions


def redact_diff(diff_text: str) -> tuple[str, dict]:
    """
    Redact secrets from a unified diff while preserving diff structure.

    Args:
        diff_text: Unified diff text (output from git diff)

    Returns:
        Tuple of (redacted_diff, stats_dict)
        where stats_dict contains redaction statistics

    The diff structure is preserved:
    - Lines starting with - or + are processed for secrets
    - Context lines and headers are kept as-is
    - Redaction markers replace actual secret values
    """
    if not diff_text:
        return diff_text, {"redacted": False, "secrets_found": 0}

    lines = diff_text.split('\n')
    redacted_lines = []
    total_redactions = {}

    for line in lines:
        # Process only diff content lines (those starting with +/- for added/removed code)
        if line.startswith(('+', '-')) and not line.startswith(('+++', '---')):
            redacted_line, redactions = redact_secrets(line)
            # Track which secrets were found
            for secret_type, count in redactions:
                total_redactions[secret_type] = total_redactions.get(secret_type, 0) + count
            redacted_lines.append(redacted_line)
        else:
            # Keep context/header lines unchanged
            redacted_lines.append(line)

    redacted_diff = '\n'.join(redacted_lines)

    stats = {
        "redacted": len(total_redactions) > 0,
        "secrets_found": sum(total_redactions.values()),
        "secrets_by_type": total_redactions,
    }

    return redacted_diff, stats


def should_redact_for_llm(repo_name: str, explicit_allow: Optional[list] = None) -> bool:
    """
    Check if this repository should have secrets redacted before LLM analysis.

    Args:
        repo_name: Repository name/path
        explicit_allow: List of repo names that can share secrets (e.g., test repos)

    Returns:
        True if secrets should be redacted (default), False if allowed to send

    Most repos should have redaction enabled. Only explicitly allow
    test/demo repositories that contain no real credentials.
    """
    if explicit_allow is None:
        explicit_allow = []

    # Never redact for these test repos
    for allowed in explicit_allow:
        if allowed.lower() in repo_name.lower():
            return False

    # Redact for everything else
    return True


# Logging/reporting functions for transparency

def redaction_summary(redactions: list[tuple[str, int]]) -> str:
    """
    Generate a human-readable summary of redactions.

    Args:
        redactions: List from redact_secrets() return value

    Returns:
        String like "Redacted: AWS Key (1), GitHub Token (2)"
    """
    if not redactions:
        return "No secrets redacted"

    parts = [f"{secret_type} ({count})" for secret_type, count in redactions]
    return f"Redacted: {', '.join(parts)}"


# Example usage:
if __name__ == "__main__":
    # Test code with credentials
    sample_code = '''
    # AWS config
    - aws_key = "AKIAIOSFODNN7EXAMPLE"
    + aws_key = os.environ.get("AWS_KEY")

    - github_token = "ghp_1234567890abcdefghijklmnopqrst"
    + github_token = os.environ.get("GITHUB_TOKEN")

    password = "MySecurePassword123"
    '''

    print("BEFORE REDACTION:")
    print(sample_code)
    print("\n" + "="*60 + "\n")

    redacted, stats = redact_diff(sample_code)
    print("AFTER REDACTION:")
    print(redacted)
    print("\n" + "="*60 + "\n")
    print("STATS:", stats)
    print("SUMMARY:", redaction_summary(stats.get("secrets_by_type", {}).items()))
