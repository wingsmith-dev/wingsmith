-- Argo CD health for plane.wingsmith.dev/Workload (ADR 0042). Install under
-- resource.customizations.health.plane.wingsmith.dev_Workload in argocd-cm.
local hs = {status = "Progressing", message = "waiting for the operator"}
if obj.status == nil or obj.status.conditions == nil then
  return hs
end
local cond = {}
for _, c in ipairs(obj.status.conditions) do
  cond[c.type] = c
end
if obj.metadata.generation ~= nil and obj.status.observedGeneration ~= obj.metadata.generation then
  hs.message = "waiting for the plane to observe this generation"
  return hs
end
if cond.Stalled ~= nil and cond.Stalled.status == "True" then
  return {status = "Degraded", message = cond.Stalled.message}
end
if cond.Synced ~= nil and cond.Synced.status == "False" then
  return {status = "Degraded", message = cond.Synced.message}
end
if cond.Ready ~= nil and cond.Ready.status == "True" then
  return {status = "Healthy", message = cond.Ready.message}
end
if cond.Synced ~= nil then
  hs.message = cond.Synced.message
end
return hs
