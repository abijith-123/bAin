import assert from "node:assert/strict";
import { after, test } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
after(() => server.close());
const { ExceptionInboxView } = await server.ssrLoadModule("/src/pages/ExceptionInboxView.tsx");
const { AiAssistantView } = await server.ssrLoadModule("/src/pages/AiAssistantView.tsx");
const { clearTurnApi } = await server.ssrLoadModule("/src/lib/api.ts");
const render = (component, props) => renderToStaticMarkup(createElement(component, props));

test("Inbox presents ownership, remaining time and admin-only controls", () => {
  const review = { exception_id: "ex-ins-402", patient_id: "402", department: "insurance",
    status: "open", owner_role: "billing_specialist", sla_minutes: 30,
    created_at: "2026-11-20T10:00:00Z", as_of: "2026-11-20T10:10:00Z" };
  const inbox = role => render(ExceptionInboxView, {activeRole: role, exceptions: [review]});
  assert.match(inbox("admin"), /Advance demo clock/);
  assert.match(inbox("admin"), /20 min remaining/);
  assert.doesNotMatch(inbox("billing_specialist"), /Advance demo clock/);
  review.owner_role = "billing_supervisor";
  assert.match(inbox("billing_supervisor"), /Escalated/);
  assert.match(inbox("billing_supervisor"), /billing supervisor/);
});

test("Clinical note form is role scoped", () => {
  assert.match(render(AiAssistantView,{activeRole:"nurse"}), /Synthetic handoff note/);
  assert.doesNotMatch(render(AiAssistantView,{activeRole:"billing_specialist"}), /textarea/);
});

test("Analysis submits only to the advisory endpoint and preserves source labeling", async t => {
  for (const mode of ["llm","deterministic_fallback"]) {
    const request = t.mock.method(globalThis, "fetch", async (url, options) => {
      assert.equal(url, "/api/agent/analyze");
      assert.equal(JSON.parse(options.body).handoff_note,"synthetic note");
      return Response.json({generation_mode:mode, note_status:"accepted"});
    });
    assert.equal((await clearTurnApi.analyzeCase("token",{patient_id:"402",handoff_note:"synthetic note"})).generation_mode,mode);
    assert.equal(request.mock.callCount(),1);
    request.mock.restore();
  }
});

test("Clock client returns reassigned IDs and surfaces API conflicts", async t => {
  const req=t.mock.method(globalThis,"fetch", async () => Response.json({reassigned_exceptions:[{exception_id:"ex-ins-402"}]}));
  assert.equal((await clearTurnApi.advanceClock("token",1,30,"test-clock")).reassigned_exceptions[0].exception_id,"ex-ins-402");
  req.mock.mockImplementation(async () => Response.json({code:"STALE_VERSION",message:"Refresh the demo clock."},{status:409}));
  await assert.rejects(clearTurnApi.advanceClock("token",1,30,"test-stale"),/Refresh/);
});

test("Wait explanation is offered to the three supported roles", () => {
  const workflow = {patient_id: "403", version: 1, milestones: [], blockers: []};
  for (const role of ["housekeeping", "transport_staff", "nurse"]) {
    assert.match(render(AiAssistantView, {activeRole: role, workflow}), /Why is this patient waiting/);
  }
  assert.doesNotMatch(render(AiAssistantView, {activeRole: "doctor", workflow}), /Why is this patient waiting/);
});
