import os
import re
import time
from typing import Mapping

def apply_patches():
    """
    Patch spotapi to include automatic retries and session refreshes for client token fetching.
    """
    try:
        import spotapi.client
        from spotapi.client import BaseClient, BaseClientError, _Undefined

        original_get_client_token = BaseClient.get_client_token

        def robust_get_client_token(self) -> None:
            last_err = None
            for attempt in range(5):
                try:
                    if not (self.client_id and self.device_id and self.client_version) or attempt > 0:
                        try:
                            self.get_session()
                        except Exception as session_err:
                            last_err = f"Session fetch error: {session_err}"
                            time.sleep(1.0 * (attempt + 1))
                            continue

                    url = "https://clienttoken.spotify.com/v1/clienttoken"
                    payload = {
                        "client_data": {
                            "client_version": self.client_version,
                            "client_id": self.client_id,
                            "js_sdk_data": {
                                "device_brand": "unknown",
                                "device_model": "unknown",
                                "os": "windows",
                                "os_version": "NT 10.0",
                                "device_id": self.device_id,
                                "device_type": "computer",
                            },
                        }
                    }
                    headers = {
                        "Authority": "clienttoken.spotify.com",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    }

                    resp = self.client.post(url, json=payload, headers=headers)

                    if resp.fail:
                        last_err = resp.error.string if hasattr(resp, 'error') and hasattr(resp.error, 'string') else str(getattr(resp, 'error', 'Request failed'))
                        time.sleep(1.0 * (attempt + 1))
                        continue

                    if not isinstance(resp.response, Mapping):
                        last_err = "Invalid JSON mapping"
                        time.sleep(1.0 * (attempt + 1))
                        continue

                    if resp.response.get("response_type") != "RESPONSE_GRANTED_TOKEN_RESPONSE":
                        last_err = resp.response.get("response_type")
                        time.sleep(1.0 * (attempt + 1))
                        continue

                    self.client_token = resp.response["granted_token"]["token"]
                    return
                except Exception as e:
                    last_err = str(e)
                    time.sleep(1.0 * (attempt + 1))

            raise BaseClientError("Could not get client token after multiple retries", error=last_err)

        BaseClient.get_client_token = robust_get_client_token
        print("✅ Patched spotapi.client.BaseClient.get_client_token with retry logic.")
    except Exception as e:
        print(f"⚠️ Could not patch spotapi: {e}")

if __name__ == "__main__":
    apply_patches()
