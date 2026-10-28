// Deployment settings, read before the app starts. This default sets nothing, so the build-time
// VITE_* values (or same-origin) apply. The standalone nginx image rewrites this file at
// container start from WINGSMITH_API_BASE_URL and WINGSMITH_READ_ONLY; see docker/40-wingsmith-config.sh.
window.__WINGSMITH_CONFIG__ = {}
