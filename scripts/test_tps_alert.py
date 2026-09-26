#!/usr/bin/env python3
import os
import unittest
from unittest import mock
import tps_alert


class TestTpsAlert(unittest.TestCase):
    def test_alert_without_webhook_logs_to_stderr(self):
        with mock.patch.dict(os.environ, {"TPS_ALERT_WEBHOOK": ""}):
            result = tps_alert.send_alert("测试", "无 Webhook 内容", level="INFO")
            self.assertFalse(result)

    @mock.patch("urllib.request.urlopen")
    def test_alert_send_dingtalk_payload(self, mock_urlopen):
        mock_resp = mock.MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        webhook = "https://oapi.dingtalk.com/robot/send?access_token=dummy"
        tps_alert._LAST_ALERT_TIMES.clear()
        ok = tps_alert.send_alert("队列积压", "DLQ > 100", level="ERROR", webhook_url=webhook)
        self.assertTrue(ok)
        self.assertEqual(mock_urlopen.call_count, 1)

        # 验证防抖节流
        ok2 = tps_alert.send_alert("队列积压", "DLQ > 100", level="ERROR", webhook_url=webhook)
        self.assertFalse(ok2)  # 因 cooldown 被节流
        self.assertEqual(mock_urlopen.call_count, 1)


if __name__ == "__main__":
    unittest.main()
