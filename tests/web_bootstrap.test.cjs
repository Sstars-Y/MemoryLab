const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

for (const withToken of [true, false]) {
  test(`page initializes and sends authenticated changes (${withToken ? 'launch link' : 'reload'})`, async () => {
    const elements = new Map();
    function element() {
      return {
        textContent: '', value: '', children: [], dataset: {},
        type: 'text', reportValidity() { return true; },
        classList: {toggle() {}, add() {}, remove() {}},
        addEventListener(name, handler) { this[name] = handler; },
        setAttribute() {}, replaceChildren(...nodes) { this.children = nodes; },
        append(...nodes) { this.children.push(...nodes); },
        prepend(...nodes) { this.children.unshift(...nodes); },
      };
    }
    const requests = [];
    const sessions = [];
    const storage = new Map();
    const browserHistory = {replaceState() { browserHistory.cleaned = true; }};
    const settings = {mode:'fake',base_url:'',model:'',summary_model:'',context_limit:8192,output_limit:1024,request_timeout:180,thinking_mode:'default',vision:false,audio:false,vision_model:'',asr_model:''};
    let active = false, hasKey = false;
    const context = vm.createContext({
      document: {
        getElementById(id) {
          if (!elements.has(id)) {
            const value = element();
            if (['vision','audio'].includes(id)) value.type = 'checkbox';
            if (['context_limit','output_limit','request_timeout'].includes(id)) value.type = 'number';
            if (id === 'api_key') value.type = 'password';
            elements.set(id, value);
          }
          return elements.get(id);
        },
        querySelectorAll() { return []; },
        createElement: element, createDocumentFragment: element,
      },
      history: browserHistory,
      location: {hash: withToken ? '#token=launch-token' : '', pathname: '/'},
      sessionStorage: {getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)},
      URL, URLSearchParams, setTimeout, crypto: {randomUUID: () => 'synthetic-request'},
      fetch: async (url, init) => {
        requests.push({url, init});
        let result;
        if (init.method === 'POST' && url !== '/api/bootstrap') {
          assert.equal(init.headers['X-CSRF-Token'], 'csrf-test', 'every modifying request needs the loaded token');
        }
        if (url === '/api/bootstrap') result = {ok: true};
        else if (url === '/api/state') result = {csrf: 'csrf-test', provider: 'fake', stats: {recording: true}, sessions, embedding: 'offline', config: {...settings}, connection: {active,has_key:hasKey}, persona: {persona_id:'persona-test',name:'Test',instructions:''}};
        else if (url === '/api/sessions') { sessions.push({id: 'session-test', title: 'Test'}); result = {id: 'session-test'}; }
        else if (url === '/api/sessions/session-test') result = {messages: [], temporary: []};
        else if (url === '/api/chat') result = {request_id: 'request-test'};
        else if (url.startsWith('/api/requests/request-test?')) result = {status: 'completed', recording: true};
        else if (url === '/api/recording') result = {ok: true};
        else if (url === '/api/config/models') result = {models:['model-a','model-b'],base_url:'https://model.example/v1'};
        else if (url === '/api/config/test') {
          if (JSON.parse(init.body).base_url.includes('failure.example')) return {ok:false,json:async()=>({error:'密钥无效，请重新填写'})};
          result = {models:['model-a'],latency_ms:25,base_url:'https://model.example/v1'};
        }
        else if (url === '/api/config') {
          const input=JSON.parse(init.body);
          for (const key of Object.keys(settings)) settings[key]=input[key];
          active=settings.mode==='remote';hasKey=active&&(Boolean(input.api_key)||hasKey);result={status:'saved'};
        }
        else if (url === '/api/config/credentials/clear') {settings.mode='fake';active=false;hasKey=false;result={status:'cleared'};}
        else throw Error(`Unexpected request: ${url}`);
        return {ok: true, json: async () => result};
      },
    });
    context.window = context;
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../memory_lab/web/app.js'), 'utf8'), context);
    for (let i = 0; i < 10; i++) await new Promise(resolve => setImmediate(resolve));
    assert.strictEqual(context.history, browserHistory, 'conversation loading must preserve the browser History API');
    assert.equal(Boolean(browserHistory.cleaned), withToken);
    assert.ok(requests.some(r => r.url === '/api/sessions/session-test'), elements.get('status')?.textContent);
    elements.get('message').value = 'Synthetic message';
    await elements.get('composer').submit({preventDefault() {}});
    assert.ok(requests.some(r => r.url === '/api/chat'));
    assert.equal(elements.get('status').textContent, '回答已完成并保存');
    await elements.get('recording').onclick();
    assert.ok(requests.some(r => r.url === '/api/recording'));

    vm.runInContext('fillSettings()', context);
    const get = id => context.document.getElementById(id);
    assert.equal(get('remote-fields').disabled, true);
    get('mode').value='remote';get('mode').change();
    assert.equal(get('remote-fields').disabled, false);
    get('base_url').value='https://model.example/v1/chat/completions';get('base_url').input();
    assert.match(get('endpoint-preview').textContent, /\/v1\/chat\/completions$/);
    get('api_key').value='test-memory-key';get('toggle-key').onclick();
    assert.equal(get('api_key').type, 'text');
    await get('load-models').onclick();
    assert.equal(get('model-options').children.length, 2);
    assert.equal(get('base_url').value, 'https://model.example/v1');
    get('model').value='model-a';
    await get('test-config').onclick();
    assert.match(get('config-feedback').textContent, /回复测试通过/);
    assert.equal(active, false, 'a probe must not switch the active provider');
    await get('config-form').onsubmit({preventDefault() {}});
    assert.equal(active, true);
    assert.equal(get('api_key').value, '');
    assert.equal(get('api_key').type, 'password');
    assert.match(get('key-status').textContent, /留空沿用/);
    get('output_limit').value='512';get('config-form').input();
    get('request_timeout').value='300';
    get('thinking_mode').value='off';
    vm.runInContext('fillSettings()', context);
    assert.equal(get('output_limit').value, '512', 'navigation must retain unsaved settings');
    await get('config-form').onsubmit({preventDefault() {}});
    const saved=JSON.parse(requests.findLast(r=>r.url==='/api/config').init.body);
    assert.equal(saved.output_limit, 512);
    assert.equal(saved.request_timeout, 300);
    assert.equal(saved.thinking_mode, 'off');
    assert.equal(saved.api_key, '');assert.equal(saved.use_existing_key, true);
    get('base_url').value='https://failure.example/v1';get('base_url').input();
    assert.match(get('key-status').textContent, /不会沿用旧密钥/);
    await get('test-config').onclick();
    assert.match(get('config-feedback').textContent, /密钥无效/);
    assert.equal(get('save-config').disabled, false, 'failures must restore form controls');
    await get('clear-key').onclick();
    assert.equal(active, false);assert.equal(hasKey, false);
    assert.equal(get('mode').value, 'fake');

    vm.runInContext('delete state.connection; renderConnectionState()', context);
    assert.match(get('connection-status').textContent, /后端尚未更新/);
    get('mode').value='remote';get('mode').change();
    assert.equal(get('load-models').disabled, true);
    assert.equal(get('save-config').disabled, true);
    vm.runInContext('state.connection={active:false,has_key:false}', context);
    context.fetch=async()=>({ok:false,status:404,json:async()=>({detail:'Not Found'})});
    await get('load-models').onclick();
    assert.match(get('config-feedback').textContent, /重新双击启动实验版/);
    context.fetch=async()=>({ok:false,status:502,json:async()=>({error:'获取模型列表失败（HTTP 401）：密钥无效'})});
    await get('load-models').onclick();
    assert.match(get('config-feedback').textContent, /HTTP 401.*密钥无效/);
  });
}
