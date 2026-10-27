import os
import unittest
from unittest.mock import patch

from core.bilibili_api import BilibiliAPI
from utils.helpers import resolve_llm_url


class ProxyConfigurationTest(unittest.TestCase):
    @patch('core.bilibili_api.load_config', return_value={})
    def test_no_unrequested_proxy(self, _load_config):
        with patch.dict(os.environ, {}, clear=True):
            client = BilibiliAPI()
            self.assertEqual(client.session.proxies, {})
            client.session.close()

    @patch('core.bilibili_api.load_config', return_value={})
    def test_explicit_https_proxy_is_preserved(self, _load_config):
        with patch.dict(os.environ, {'HTTPS_PROXY': 'http://proxy.example:8080'}, clear=True):
            client = BilibiliAPI()
            self.assertEqual(client.session.proxies['https'], 'http://proxy.example:8080')
            self.assertNotIn('http', client.session.proxies)
            client.session.close()


class WslAddressConfigurationTest(unittest.TestCase):
    @patch('utils.helpers._is_wsl', return_value=True)
    @patch('utils.helpers.get_wsl_host_ip')
    def test_wsl_keeps_configured_url_by_default(self, get_host, _is_wsl):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(resolve_llm_url('http://127.0.0.1:4000/v1'), 'http://127.0.0.1:4000/v1')
            get_host.assert_not_called()

    @patch('utils.helpers._is_wsl', return_value=True)
    @patch('utils.helpers.get_wsl_host_ip', return_value='192.0.2.1')
    def test_wsl_nameserver_resolution_is_explicit(self, _get_host, _is_wsl):
        with patch.dict(os.environ, {'BILIBILI_ANALYZER_LLM_RESOLVE_WSL': '1'}, clear=True):
            self.assertEqual(resolve_llm_url('http://127.0.0.1:4000/v1'), 'http://192.0.2.1:4000/v1')

    @patch('utils.helpers._is_wsl', return_value=True)
    @patch('utils.helpers.get_wsl_host_ip', return_value=None)
    def test_missing_wsl_nameserver_reports_error_if_opted_in(self, _get_host, _is_wsl):
        with patch.dict(os.environ, {'BILIBILI_ANALYZER_LLM_RESOLVE_WSL': '1'}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'configure llm.base_url explicitly'):
                resolve_llm_url('http://127.0.0.1:4000/v1')


if __name__ == '__main__':
    unittest.main()
