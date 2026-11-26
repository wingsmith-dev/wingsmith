// Deployment settings, read before the app starts. This default sets nothing, so the build-time
// VITE_* values (or same-origin) apply. The standalone nginx image rewrites this file at
// container start from WINGSMITH_API_BASE_URL, WINGSMITH_READ_ONLY and WINGSMITH_DEMO_PASSWORD; see docker/40-wingsmith-config.sh.
window.__WINGSMITH_CONFIG__ = {}
