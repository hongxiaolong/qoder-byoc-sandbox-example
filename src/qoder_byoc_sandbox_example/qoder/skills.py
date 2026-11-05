# SPDX-License-Identifier: Apache-2.0
"""Skills：打包、上传、固定版本下载并校验内容。"""

import io
import json
import uuid
import zipfile


def upload(client, run_id, skill_name, skill_file, on_created=lambda skill: None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(skill_file, f"{skill_name}/SKILL.md")
    boundary = "demo-" + uuid.uuid4().hex
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="metadata"\r\n\r\n'
        + json.dumps({"demo_run": run_id})
        + "\r\n"
    ).encode()
    body += (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{skill_name}.zip"\r\nContent-Type: application/zip\r\n\r\n'
        ).encode()
        + buffer.getvalue()
        + f"\r\n--{boundary}--\r\n".encode()
    )
    skill = client.request(
        "POST",
        "/skills",
        body,
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary},
    )
    on_created(skill)
    data = client.request(
        "GET",
        f"/skills/{skill['id']}/versions/{skill['latest_version']}/content",
        raw=True,
    )
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        downloaded = z.read(skill_name + "/SKILL.md")
    if downloaded != skill_file.read_bytes():
        raise ValueError("Qoder Skill version differs from source")
    return skill, downloaded
