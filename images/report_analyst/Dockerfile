FROM fc-e2b-registry.cn-hangzhou.cr.aliyuncs.com/runtime-v2/base:v0.0.53@sha256:57573087625efc9ff4713c03bff43adc2feb286b407d3d65a70729e443760e5d
USER root

WORKDIR /opt/demo
COPY images/report_analyst/requirements.runtime.txt /opt/demo/requirements.runtime.txt
RUN python3 -m pip install --no-cache-dir -r /opt/demo/requirements.runtime.txt
COPY src /opt/demo/src
ENV PYTHONPATH=/opt/demo/src PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN mkdir -p /workspace/artifacts /workspace/evidence
# Start/ready commands are configured on the Sandbox template, not as a Docker daemon dependency.
CMD ["python3", "-m", "qoder_byoc_sandbox_example.demos.report_analyst.service", "--root", "/workspace", "--port", "8000"]
