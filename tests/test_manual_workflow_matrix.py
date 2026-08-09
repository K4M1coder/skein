import tempfile
import unittest
from pathlib import Path

from scripts import manual_workflow_matrix as matrix


class ManualWorkflowMatrixTest(unittest.TestCase):
    def test_output_is_created_before_execution_and_final_analysis_is_written(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = matrix.prepare_output_dir(Path(temporary) / "results")
            self.assertTrue(session.is_absolute())
            self.assertTrue(session.is_dir())
            result = {
                "template_name": "Example", "difficulty": "simple", "status": "HARNESS_ERROR",
                "elapsed_seconds": 0.1, "summary": {}, "execution_report": None,
                "artifact_count": 0, "execution_log_count": 0, "collection_errors": [], "error": "expected",
            }
            matrix.atomic_json(session / "results.partial.json", [result])
            json_path, markdown_path = matrix.write_final(session, [result])
            self.assertTrue(json_path.is_file())
            self.assertTrue(markdown_path.is_file())
            self.assertIn("HARNESS_ERROR", markdown_path.read_text(encoding="utf-8"))

    def test_analysis_counts_failures_without_aborting(self):
        analysis = matrix.analyze_results([
            {"status": "FAILED", "template_name": "One", "difficulty": "complex", "summary": {}, "failed_tasks": ["step"]},
            {"status": "COMPLETED", "template_name": "Two", "difficulty": "simple", "summary": {"total_tokens": 12, "completion_tokens": 5, "wall_clock_seconds": 2, "energy_wh": 0.2}, "execution_report": "report"},
        ])
        self.assertEqual(analysis["cases"], 2)
        self.assertEqual(analysis["failed"], 1)
        self.assertEqual(analysis["completed"], 1)
        self.assertEqual(analysis["total_tokens"], 12)


if __name__ == "__main__":
    unittest.main()
