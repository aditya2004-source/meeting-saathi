import test from "node:test";
import assert from "node:assert/strict";

import {
  buildTranscript,
  computeAttendees,
  distinctSpeakers,
  fillUnresolvedWithExcerpts,
  formatMeetingDate,
  formatTimestamp,
  isPlaceholderSpeaker,
  parseAttendeeRoster,
  parseSpeakerEvents,
  renameSpeakers,
  renderPlainText,
  resolveSpeakerNames,
} from "../transcript.js";

const seg = (start, end, speaker, text) => ({ start, end, speaker, text });

// --- parseSpeakerEvents (port of tests/test_speaker_names.py) ---

test("parseSpeakerEvents happy path", () => {
  const raw = '[{"name": "Priya Shah", "t_seconds": 12.5}, {"name": "Rahul", "t_seconds": 40}]';
  assert.deepEqual(parseSpeakerEvents(raw), [
    { name: "Priya Shah", tSeconds: 12.5 },
    { name: "Rahul", tSeconds: 40 },
  ]);
});

test("parseSpeakerEvents filters You", () => {
  assert.deepEqual(parseSpeakerEvents('[{"name": "You", "t_seconds": 1.0}, {"name": "you", "t_seconds": 2.0}]'), []);
});

test("parseSpeakerEvents malformed input returns empty", () => {
  assert.deepEqual(parseSpeakerEvents("not json"), []);
  assert.deepEqual(parseSpeakerEvents('{"not": "a list"}'), []);
  assert.deepEqual(parseSpeakerEvents('[{"name": null, "t_seconds": 1}]'), []);
  assert.deepEqual(parseSpeakerEvents('[{"name": "Priya", "t_seconds": "oops"}]'), []);
});

// --- resolveSpeakerNames ---

test("resolveSpeakerNames with no events is a no-op", () => {
  const segments = [seg(0, 1, "Speaker 1", "hi")];
  assert.deepEqual(resolveSpeakerNames(segments, []), segments);
});

test("resolveSpeakerNames majority vote picks the correct name", () => {
  const segments = [seg(0, 2, "Speaker 1", "a"), seg(5, 7, "Speaker 1", "b")];
  const events = parseSpeakerEvents(
    '[{"name":"Priya Shah","t_seconds":1},{"name":"Priya Shah","t_seconds":6},{"name":"Rahul","t_seconds":6.2}]',
  );
  const resolved = resolveSpeakerNames(segments, events);
  assert.deepEqual(resolved.map((s) => s.speaker), ["Priya Shah", "Priya Shah"]);
  assert.deepEqual(resolved.map((s) => s.text), ["a", "b"]);
});

test("resolveSpeakerNames below confidence falls back to the placeholder", () => {
  const segments = [seg(0, 1, "Speaker 1", "a")];
  const events = parseSpeakerEvents('[{"name":"Priya Shah","t_seconds":0.5},{"name":"Rahul","t_seconds":0.6}]');
  assert.equal(resolveSpeakerNames(segments, events, { minConfidence: 0.75 })[0].speaker, "Speaker 1");
});

test("resolveSpeakerNames respects the tolerance window", () => {
  const segments = [seg(10, 12, "Speaker 1", "a")];
  const events = parseSpeakerEvents('[{"name":"Priya Shah","t_seconds":12.5}]');
  assert.equal(resolveSpeakerNames(segments, events, { toleranceSeconds: 1.0 })[0].speaker, "Priya Shah");
  assert.equal(resolveSpeakerNames(segments, events, { toleranceSeconds: 0.1 })[0].speaker, "Speaker 1");
});

test("resolveSpeakerNames never touches Unknown", () => {
  const segments = [seg(0, 1, "Unknown", "a")];
  const events = parseSpeakerEvents('[{"name":"Priya Shah","t_seconds":0.5}]');
  assert.equal(resolveSpeakerNames(segments, events)[0].speaker, "Unknown");
});

test("resolveSpeakerNames collision merges both groups", () => {
  const segments = [seg(0, 1, "Speaker 1", "a"), seg(10, 11, "Speaker 2", "b")];
  const events = parseSpeakerEvents(
    '[{"name":"Priya Shah","t_seconds":0.2},{"name":"Priya Shah","t_seconds":0.4},' +
      '{"name":"Priya Shah","t_seconds":0.6},{"name":"Priya Shah","t_seconds":10.2}]',
  );
  const resolved = resolveSpeakerNames(segments, events);
  assert.deepEqual(resolved.map((s) => s.speaker), ["Priya Shah", "Priya Shah"]);
});

test("resolveSpeakerNames normalizes votes across name variants", () => {
  const segments = [seg(0, 2, "Speaker 1", "a")];
  const events = parseSpeakerEvents(
    '[{"name":"Priya Shah","t_seconds":0.2},{"name":"priya  shah","t_seconds":0.4},{"name":"Priya Shah (Host)","t_seconds":0.6}]',
  );
  assert.equal(resolveSpeakerNames(segments, events)[0].speaker, "Priya Shah");
});

test("resolveSpeakerNames never touches an already-real name", () => {
  const segments = [seg(0, 1, "Priya Shah", "already named"), seg(10, 11, "Speaker 1 (chunk@10.0)", "fallback")];
  const events = parseSpeakerEvents('[{"name":"Rahul","t_seconds":0.5},{"name":"Rahul","t_seconds":10.2}]');
  const resolved = resolveSpeakerNames(segments, events);
  assert.equal(resolved[0].speaker, "Priya Shah");
  assert.equal(resolved[1].speaker, "Rahul");
});

test("isPlaceholderSpeaker", () => {
  assert.ok(isPlaceholderSpeaker("Unknown"));
  assert.ok(isPlaceholderSpeaker("Speaker 1"));
  assert.ok(isPlaceholderSpeaker("Speaker 12 (chunk@123.4)"));
  assert.ok(!isPlaceholderSpeaker("Priya Shah"));
  assert.ok(!isPlaceholderSpeaker("Unidentified speaker 1"));
});

// --- fillUnresolvedWithExcerpts ---

test("fillUnresolvedWithExcerpts groups one placeholder into one label", () => {
  const segments = [
    seg(0, 1, "Speaker 1", "hello everyone"),
    seg(2, 3, "Speaker 1", "let's get started"),
    seg(5, 6, "Priya Shah", "already resolved"),
  ];
  const { segments: filled, excerpts } = fillUnresolvedWithExcerpts(segments);
  assert.equal(filled[0].speaker, "Unidentified speaker 1");
  assert.equal(filled[1].speaker, "Unidentified speaker 1");
  assert.equal(filled[2].speaker, "Priya Shah");
  assert.ok(excerpts["Unidentified speaker 1"].includes("hello everyone"));
});

test("fillUnresolvedWithExcerpts gives each Unknown its own label", () => {
  const segments = [seg(0, 1, "Unknown", "first unknown line"), seg(5, 6, "Unknown", "second unknown line")];
  const { segments: filled } = fillUnresolvedWithExcerpts(segments);
  assert.equal(filled[0].speaker, "Unidentified speaker 1");
  assert.equal(filled[1].speaker, "Unidentified speaker 2");
});

// --- roster (port of tests/test_roster.py) ---

test("parseAttendeeRoster flat strings and dict objects", () => {
  assert.deepEqual(parseAttendeeRoster('["Priya Shah", "Rahul Verma"]'), ["Priya Shah", "Rahul Verma"]);
  assert.deepEqual(parseAttendeeRoster('[{"name": "Priya Shah"}, {"name": "Rahul Verma"}]'), ["Priya Shah", "Rahul Verma"]);
});

test("parseAttendeeRoster filters You and dedupes variants", () => {
  assert.deepEqual(parseAttendeeRoster('["You", "you", "Priya Shah", "Priya Shah"]'), ["Priya Shah"]);
  assert.deepEqual(parseAttendeeRoster('["Priya Shah", "priya  shah", "Priya Shah (Host)"]'), ["Priya Shah"]);
});

test("parseAttendeeRoster malformed input returns empty", () => {
  assert.deepEqual(parseAttendeeRoster("not json"), []);
  assert.deepEqual(parseAttendeeRoster('{"not": "a list"}'), []);
  assert.deepEqual(parseAttendeeRoster("[42, true, null]"), []);
});

test("computeAttendees keeps roster order and adds off-roster spoken names", () => {
  assert.deepEqual(
    computeAttendees(["Priya Shah", "Rahul Verma", "Silent Person"], [seg(0, 1, "Priya Shah", "hi")]),
    ["Priya Shah", "Rahul Verma", "Silent Person"],
  );
  assert.deepEqual(
    computeAttendees(["Priya Shah"], [seg(0, 1, "Priya Shah", "hi"), seg(2, 3, "External Dial-In", "hello")]),
    ["Priya Shah", "External Dial-In"],
  );
});

test("computeAttendees never includes unresolved placeholders", () => {
  assert.deepEqual(
    computeAttendees(["Priya Shah"], [seg(0, 1, "Speaker 1", "hi"), seg(2, 3, "Unknown", "hello")]),
    ["Priya Shah"],
  );
});

test("computeAttendees dedupes roster vs spoken-name variants", () => {
  assert.deepEqual(computeAttendees(["Priya Shah (Host)"], [seg(0, 1, "priya shah", "hi")]), ["Priya Shah (Host)"]);
});

// --- merge (port of tests/test_merge.py) ---

test("formatMeetingDate renders IST by default", () => {
  assert.equal(formatMeetingDate("2026-07-28T09:00:00+00:00"), "28 July 2026, 2:30 PM IST");
});

test("formatMeetingDate returns malformed input unchanged", () => {
  assert.equal(formatMeetingDate("not a date"), "not a date");
  assert.equal(formatMeetingDate(""), "");
});

test("formatTimestamp", () => {
  assert.equal(formatTimestamp(0), "00:00:00");
  assert.equal(formatTimestamp(3661), "01:01:01");
});

test("renderPlainText", () => {
  const out = renderPlainText({ meetingTitle: "Sync", segments: [seg(0, 1, "Priya", "hi"), seg(65, 70, "Rahul", "bye")] });
  assert.equal(out, "Sync\n\n[00:00:00] Priya: hi\n[00:01:05] Rahul: bye");
});

// --- buildTranscript end-to-end ---

test("buildTranscript resolves Gemini generic labels via DOM events and computes attendees", () => {
  const raw = {
    segments: [
      { start_seconds: 0, end_seconds: 4, speaker: "Speaker 1", text: "Welcome everyone." },
      { start_seconds: 5, end_seconds: 9, speaker: "Speaker 2", text: "Thanks, glad to be here." },
      { start_seconds: 10, end_seconds: 12, speaker: "Speaker 1", text: "Let's start." },
    ],
    attendees: ["Speaker 1", "Speaker 2"],
  };
  const t = buildTranscript(raw, {
    meetingTitle: "Kickoff",
    speakerEvents: [
      { name: "Aditya", t_seconds: 0.5 },
      { name: "Aditya", t_seconds: 11 },
      { name: "Neha", t_seconds: 6 },
    ],
    roster: ["Aditya", "Neha", "Silent Sam"],
  });
  assert.deepEqual(t.segments.map((s) => s.speaker), ["Aditya", "Neha", "Aditya"]);
  assert.deepEqual(t.attendees, ["Aditya", "Neha", "Silent Sam"]);
  assert.ok(t.plainText.startsWith("Kickoff\n\n[00:00:00] Aditya: Welcome everyone."));
});

test("buildTranscript labels unmatched generic speakers as Unidentified speaker N", () => {
  const raw = { segments: [{ start_seconds: 0, end_seconds: 2, speaker: "Speaker 1", text: "Anonymous voice." }] };
  const t = buildTranscript(raw, { meetingTitle: "M", speakerEvents: [], roster: [] });
  assert.equal(t.segments[0].speaker, "Unidentified speaker 1");
  assert.deepEqual(t.attendees, []);
});

// --- distinctSpeakers / renameSpeakers (dashboard "rename speakers") ---

test("distinctSpeakers returns first-appearance order", () => {
  const segs = [seg(0, 1, "B", "x"), seg(1, 2, "A", "y"), seg(2, 3, "B", "z"), seg(3, 4, "C", "w")];
  assert.deepEqual(distinctSpeakers(segs), ["B", "A", "C"]);
});

test("renameSpeakers applies a label map to segments, attendees and plainText", () => {
  const transcript = {
    segments: [seg(0, 3, "Unidentified speaker 1", "Welcome."), seg(4, 7, "Unidentified speaker 2", "Thanks."), seg(8, 9, "Aditya", "Ok.")],
    attendees: ["Aditya", "Unidentified speaker 1"],
  };
  const out = renameSpeakers(transcript, { "Unidentified speaker 1": "Nirmal", "Unidentified speaker 2": "  " }, "Kickoff");
  assert.deepEqual(out.segments.map((s) => s.speaker), ["Nirmal", "Unidentified speaker 2", "Aditya"]);
  // "Aditya" + renamed "Nirmal" (was Unidentified speaker 1); blank rename ignored,
  // and the still-placeholder "Unidentified speaker 2" is not added to attendees.
  assert.deepEqual(out.attendees, ["Aditya", "Nirmal"]);
  assert.ok(out.plainText.startsWith("Kickoff\n\n[00:00:00] Nirmal: Welcome."));
});

test("renameSpeakers is a no-op for an identity / empty map", () => {
  const transcript = { segments: [seg(0, 1, "A", "hi")], attendees: ["A"] };
  const out = renameSpeakers(transcript, { A: "A" }, "M");
  assert.deepEqual(out.segments, transcript.segments);
  assert.deepEqual(out.attendees, ["A"]);
});
