const fs = require('fs')

const projectRoot = __dirname
const localPython = `${projectRoot}/.venv/bin/python`
const pythonBin = process.env.PYTHON_BIN || (fs.existsSync(localPython) ? localPython : 'python3')

module.exports = {
  apps: [
    {
      name: 'alpha-factor-service',
      cwd: projectRoot,
      script: pythonBin,
      args: ['-m', 'factor_service.main'],
      interpreter: 'none',
      autorestart: true,
      watch: false,
      max_restarts: 10,
      min_uptime: '5s',
      env: {
        PYTHONUNBUFFERED: '1',
        ...(process.env.ALPHA_REMOTE_NODE_SECRET_KEY
          ? { ALPHA_REMOTE_NODE_SECRET_KEY: process.env.ALPHA_REMOTE_NODE_SECRET_KEY }
          : {}),
      },
    },
  ],
}
