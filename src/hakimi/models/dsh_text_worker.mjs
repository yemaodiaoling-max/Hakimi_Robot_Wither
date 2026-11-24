/** Persistent DSH driver; every request owns and disposes a fresh Agent. */
import { randomUUID } from 'node:crypto';
import { connect } from 'node:net';
import { pathToFileURL } from 'node:url';
import { join } from 'node:path';
export const name = 'hakimi-text-worker';
export const inject = ['agents', 'agentDefaultModel', 'sessions', 'headlessStartup'];

export function apply(ctx) {
  async function start() {
    await ctx.get('loader')?.await();
    const root = process.env.QQ_TEXT_JUDGE_LIBRARIES;
    const { installModelSelection } = await import(pathToFileURL(join(root, 'dsh-agent/lib/index.js')));
    const { createUserMessage } = await import(pathToFileURL(join(root, 'dsh-llm/lib/index.js')));
    const socket = connect(Number(process.env.QQ_TEXT_JUDGE_PORT), '127.0.0.1');
    socket.setEncoding('utf8');
    const send = value => socket.write(JSON.stringify(value) + '\n');
    let buffer = '', chain = Promise.resolve();
    socket.on('connect', () => send({ready: true}));
    socket.on('error', () => ctx.get('appExit')(1));
    socket.on('close', () => ctx.get('appExit')(0));
    async function run(request) {
      let handle;
      globalThis.__HAKIMI_TEXT_USAGE = undefined;
      try {
        const selected = {...ctx.get('agentDefaultModel').currentSelection(),
                          reasoningEffort: request.thinking === 'off' ? 'off' : 'low'};
        handle = await ctx.get('agents').create({
          sessionId: `session-${randomUUID()}`, meta: {cwd: process.cwd()},
          agentOptions: {provider: selected.provider, model: selected.model,
                         reasoningEffort: selected.reasoningEffort},
          setup: agentCtx => { installModelSelection(agentCtx, {current: selected, assembled: undefined}); }
        });
        const agent = handle.agent;
        await agent.whenIdle();
        const first = agent.session.seq;
        agent.followup(createUserMessage({content: [{type: 'text', text: request.task}], source: {kind: 'user'}}));
        await agent.whenIdle();
        let text = '', reason;
        for (let seq = first; seq < agent.session.seq; seq++) {
          const event = agent.session.eventAt(seq);
          if (event?.type === 'assistant/message') {
            const value = event.data.message.content.filter(x => x.type === 'text').map(x => x.text).join('');
            if (value) text = value;
          }
          if (event?.type === 'turn/end') reason = event.data.reason;
        }
        if (reason?.kind !== 'completed') throw new Error(reason?.error?.code || 'TEXT_JUDGE_AGENT_FAILED');
        // Dispose before acknowledging: no task session survives into the next request.
        await handle.dispose(); handle = undefined;
        send({id: request.id, text, usage: globalThis.__HAKIMI_TEXT_USAGE});
      } catch (error) {
        if (handle) await handle.dispose();
        send({id: request.id, error: 'TEXT_JUDGE_AGENT_FAILED', detail: String(error).slice(0, 300), usage: globalThis.__HAKIMI_TEXT_USAGE});
      }
    }
    socket.on('data', chunk => {
      buffer += chunk.toString('utf8');
      let end;
      while ((end = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, end); buffer = buffer.slice(end + 1);
        chain = chain.then(() => run(JSON.parse(line))).catch(() => ctx.get('appExit')(1));
      }
    });
  }
  start().catch(() => ctx.get('appExit')(1));
}
