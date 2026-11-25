# SPDX-License-Identifier: GPL-3.0-or-later

from dataclasses import dataclass

from mcp_types import CallToolResult, ContentBlock, TextContent


@dataclass(frozen=True)
class ResultCap:
    """
    Limits the combined text of a tool result, telling the caller how to narrow the call.
    """

    MAX_CHARS = 200_000
    """the size that clients are told to show inline, and the size beyond which results are cut"""

    limit: int = MAX_CHARS

    def apply(self, result: CallToolResult) -> CallToolResult:
        """
        :param result: a tool result
        :return: the result itself if its text fits; otherwise a copy whose text is cut so that text and notice
            together fit, without structured content (which would repeat the cut text)
        """
        total = sum(len(block.text) for block in result.content if isinstance(block, TextContent))
        if total <= self.limit:
            return result

        notice = (
            f"\n\n[Serena router: the result had {total} characters and was cut at the limit of {self.limit}. "
            "Narrow the call with `relative_path` (a file or a folder), a lower `depth`, or `max_matches`.]"
        )
        budget = self.limit - len(notice)
        content: list[ContentBlock] = []
        for block in result.content:
            if not isinstance(block, TextContent):
                content.append(block)
            elif budget > 0:
                content.append(block.model_copy(update={"text": block.text[:budget]}))
                budget -= len(block.text)
        content.append(TextContent(text=notice))
        return result.model_copy(update={"content": content, "structured_content": None})
