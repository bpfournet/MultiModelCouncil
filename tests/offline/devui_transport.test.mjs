import assert from "node:assert/strict";
import vm from "node:vm";
import {readFileSync} from "node:fs";

const input = JSON.parse(readFileSync(0,"utf8"));
let posts=0, gets=0;
const context={Zg:async()=>({oaiMode:{enabled:false}}),Vg:()=>null,Hg:()=>{},Kg:()=>false,
  e_:3,o_:async()=>{},$g:1,URLSearchParams,TextDecoder,DOMException,console,
  fetch:async(_,options)=>{if(options.method==="POST"){posts++;throw Error("lost first reply");}gets++;
    return new Response(new ReadableStream({start(controller){controller.close();}}));}};
assert(input.stream_method.includes("automatic resend disabled"));
// Stub only the lazy store's import.meta base URL; execute the actual served
// client method, including its catch/reconnect/POST branches.
const method=input.stream_method.replaceAll("import.meta.url", "'http://127.0.0.1:8080/assets/index.js'");
vm.runInNewContext("class Client{"+method+"};client=new Client();client.baseUrl='http://127.0.0.1:8080';",context);
async function deliberateRun(){for await(const _ of context.client.streamOpenAIResponse({input:"same question"})){};}
await assert.rejects(deliberateRun,/automatic resend disabled/);
assert.equal(posts,1);
for await(const _ of context.client.streamOpenAIResponse({input:"same question"},undefined,undefined,"resp_existing")){}
assert.equal(posts,1);assert.equal(gets,1);
await assert.rejects(deliberateRun,/automatic resend disabled/);
assert.equal(posts,2,"A deliberate new Run must remain allowed; no question cooldown");
console.log("DevUI transport: PASS; lost first reply produces 1 POST, known-response reconnect uses GET only; deliberate same-question Run remains allowed");
