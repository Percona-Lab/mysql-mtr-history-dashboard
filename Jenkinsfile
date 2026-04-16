pipeline {
    agent { label 'micro-amazon' }

    parameters {
        string(name: 'LIMIT', defaultValue: '20',
               description: 'Number of recent builds to scan')
        string(name: 'RESULT_FILTER', defaultValue: 'UNSTABLE,FAILURE',
               description: 'Comma-separated build results to process')
        booleanParam(name: 'FORCE', defaultValue: false,
                     description: 'Re-process builds even if already ingested')
        booleanParam(name: 'DRY_RUN', defaultValue: false,
                     description: 'Generate OpenMetrics but do not ingest to Prometheus')
    }

    environment {
        HETZNER_HOST      = '162.55.36.239'
        HETZNER_STAGE     = '/opt/observability/backfill'
        TARGET_JOB        = 'percona-server-8.0-pipeline-parallel-mtr'
        JOB_BASE_URL      = 'https://ps80.cd.percona.com'
    }

    stages {
        stage('Setup') {
            steps {
                git url: 'https://github.com/Percona-Lab/mysql-mtr-history-dashboard.git',
                    branch: 'main'
                sh '''
                    curl -LsSf https://astral.sh/uv/install.sh | sh
                    export PATH="$HOME/.local/bin:$PATH"
                    uv sync
                '''
            }
        }

        stage('Fetch') {
            steps {
                withCredentials([
                    string(credentialsId: 'JNKPERCONA_PS80_TOKEN', variable: 'JENKINS_TOKEN'),
                    sshUserPrivateKey(credentialsId: 'MTR_DASHBOARD_HETZNER_SSH',
                                      keyFileVariable: 'SSH_KEY', usernameVariable: 'SSH_USER')
                ]) {
                    sh """
                        export PATH="\$HOME/.local/bin:\$PATH"
                        export JENKINS_USER=JNKPercona

                        # Query Prometheus for already-ingested builds (idempotency).
                        SSH_OPTS="-i \${SSH_KEY} -o StrictHostKeyChecking=no"
                        SKIP_BUILDS=\$(ssh \${SSH_OPTS} \${SSH_USER}@${HETZNER_HOST} \\
                            'curl -sf "http://127.0.0.1:9090/api/v1/query" \\
                                --data-urlencode "query=group by (build_n) (last_over_time(mtr_build_info[365d]))"' \\
                            | python3 -c 'import json,sys; d=json.load(sys.stdin); print(",".join(m["metric"]["build_n"] for m in d.get("data",{}).get("result",[])))' \\
                            2>/dev/null || echo "")
                        echo "Already ingested: \${SKIP_BUILDS:-none}"

                        uv run mtr-backfill fetch-rest \\
                            --base-url "${JOB_BASE_URL}" \\
                            --job "${TARGET_JOB}" \\
                            --limit ${params.LIMIT} \\
                            --result-filter "${params.RESULT_FILTER}" \\
                            \${SKIP_BUILDS:+--skip-builds "\${SKIP_BUILDS}"} \\
                            ${params.FORCE ? '--force' : ''}
                    """
                }
            }
        }

        stage('Export') {
            steps {
                sh '''
                    export PATH="$HOME/.local/bin:$PATH"
                    uv run mtr-backfill export
                    uv run mtr-backfill merge
                '''
            }
        }

        stage('Ingest') {
            when { expression { return !params.DRY_RUN } }
            steps {
                withCredentials([sshUserPrivateKey(
                    credentialsId: 'MTR_DASHBOARD_HETZNER_SSH',
                    keyFileVariable: 'SSH_KEY',
                    usernameVariable: 'SSH_USER')]) {
                    sh '''
                        SSH_OPTS="-i ${SSH_KEY} -o StrictHostKeyChecking=no"
                        HOST="${SSH_USER}@${HETZNER_HOST}"

                        ssh ${SSH_OPTS} ${HOST} "mkdir -p ${HETZNER_STAGE}"
                        rsync -av -e "ssh ${SSH_OPTS}" \
                            promtool/merged.openmetrics.txt \
                            ${HOST}:${HETZNER_STAGE}/merged.openmetrics.txt

                        ssh ${SSH_OPTS} ${HOST} \
                            "docker compose -f /opt/observability/compose.yml run --rm \
                             -v ${HETZNER_STAGE}:/in \
                             -v observability_prom-data:/prometheus \
                             --entrypoint promtool prometheus \
                             tsdb create-blocks-from openmetrics /in/merged.openmetrics.txt /prometheus"

                        ssh ${SSH_OPTS} ${HOST} \
                            "curl -sfS -X POST http://127.0.0.1:9090/-/reload"

                        echo "Verifying ingestion..."
                        ssh ${SSH_OPTS} ${HOST} \
                            "curl -sf 'http://127.0.0.1:9090/api/v1/query?query=count(mtr_build_info)'"
                    '''
                }
            }
        }
    }

    post {
        always { cleanWs() }
    }
}
