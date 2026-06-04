from __future__ import annotations

import unittest

from batchagent.web_tools import parse_duckduckgo_results


class WebToolsTests(unittest.TestCase):
    def test_parse_lite_duckduckgo_result(self) -> None:
        html = """
        <a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Freport" class='result-link'>Example Report</a>
        <td class='result-snippet'>A short snippet.</td>
        """
        results = parse_duckduckgo_results(html)
        self.assertEqual(results[0]["title"], "Example Report")
        self.assertEqual(results[0]["url"], "https://example.com/report")
        self.assertEqual(results[0]["snippet"], "A short snippet.")


if __name__ == "__main__":
    unittest.main()

