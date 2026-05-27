"""MLflow initialization — CR mode auth, CA bundle, LangChain autolog.

Must be imported and called BEFORE any LangChain imports so autolog
patches the classes before they're instantiated.
"""

import os
import tempfile


def _setup_ca_bundle() -> None:
    """Merge system CAs with Kubernetes service CA for TLS to MLflow gateway."""
    if os.environ.get("REQUESTS_CA_BUNDLE"):
        return
    system_ca = "/etc/pki/tls/certs/ca-bundle.crt"
    service_ca = "/var/run/secrets/kubernetes.io/serviceaccount/service-ca.crt"
    parts = []
    if os.path.isfile(system_ca):
        with open(system_ca) as f:
            parts.append(f.read())
    if os.path.isfile(service_ca):
        with open(service_ca) as f:
            parts.append(f.read())
    if parts:
        combined = tempfile.NamedTemporaryFile(
            mode="w", suffix=".crt", delete=False, prefix="ca-bundle-"
        )
        combined.write("\n".join(parts))
        combined.close()
        os.environ["REQUESTS_CA_BUNDLE"] = combined.name
        print(f"[mlflow] CA bundle: {combined.name}", flush=True)


def init() -> None:
    """Configure MLflow tracing with LangChain autolog."""
    mlflow_uri = os.environ.get("MLFLOW_TRACKING_URI", "").strip()
    if not mlflow_uri:
        return

    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        _setup_ca_bundle()

        import mlflow

        token_file = os.environ.get("MLFLOW_TRACKING_TOKEN_FILE", "").strip()
        if token_file and os.path.isfile(token_file):
            with open(token_file) as f:
                os.environ["MLFLOW_TRACKING_TOKEN"] = f.read().strip()

        mlflow.set_tracking_uri(mlflow_uri)

        workspace = os.environ.get("MLFLOW_WORKSPACE", "").strip()
        if workspace:
            mlflow.set_workspace(workspace)

        experiment_name = os.environ.get("MLFLOW_EXPERIMENT_NAME", "mlb-data-agent")

        if workspace:
            import mlflow.tracking.fluent as _fluent
            client = mlflow.MlflowClient()
            exps = client.search_experiments(
                filter_string=f"name = '{experiment_name}'"
            )
            if exps:
                _fluent._active_experiment_id = exps[0].experiment_id
            else:
                _fluent._active_experiment_id = client.create_experiment(experiment_name)
            print(f"[mlflow] Workspace={workspace}  experiment_id={_fluent._active_experiment_id}", flush=True)
        else:
            mlflow.set_experiment(experiment_name)

        mlflow.langchain.autolog()

        print(f"[mlflow] Tracing enabled (langchain autolog) -> {mlflow_uri}", flush=True)

        from prompts import register_prompts
        register_prompts()

    except Exception as exc:
        print(f"[mlflow] Failed to initialise (continuing without): {exc}", flush=True)
