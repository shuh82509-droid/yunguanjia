import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.oss_service import oss_service


def main() -> None:
    source = Path(sys.argv[1]).resolve()
    if not source.is_file():
        raise SystemExit("test file not found")
    ticket = oss_service.create_upload(source.name, "image/png", "Codex验收后删除")
    key = ticket["object_key"]
    try:
        preflight = Request(
            ticket["upload_url"],
            method="OPTIONS",
            headers={
                "Origin": "https://app.fandow.top",
                "Access-Control-Request-Method": "PUT",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        try:
            with urlopen(preflight, timeout=30) as response:
                cors_status = response.status
                allow_origin = response.headers.get("Access-Control-Allow-Origin", "")
        except HTTPError as error:
            cors_status = error.code
            allow_origin = error.headers.get("Access-Control-Allow-Origin", "")

        upload = Request(
            ticket["upload_url"],
            data=source.read_bytes(),
            method="PUT",
            headers={**ticket["headers"], "Origin": "https://app.fandow.top"},
        )
        with urlopen(upload, timeout=120) as response:
            upload_status = response.status
            upload_allow_origin = response.headers.get("Access-Control-Allow-Origin", "")
        remote = oss_service.head_asset(key)
        assert remote and remote["size"] == source.stat().st_size
        print(
            {
                "key_prefix": "/".join(key.split("/")[:3]) + "/",
                "preflight_status": cors_status,
                "preflight_allow_origin": allow_origin,
                "upload_status": upload_status,
                "upload_allow_origin": upload_allow_origin,
                "verified_size": remote["size"],
            }
        )
    finally:
        if oss_service.client:
            oss_service.client.delete_object(Bucket="marketing-video-dashboard", Key=key)
            print("temporary OSS test object deleted")


if __name__ == "__main__":
    main()
