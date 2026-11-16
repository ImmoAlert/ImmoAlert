import {browserTools} from './browser.mjs';
/** Public-site browser check against the disposable demo (fake data only).
 * Anonymous browsing works, member actions ask for an account, registration signs in.
 */
import {readFile,mkdtemp} from 'node:fs/promises';
import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {resolve,dirname} from 'node:path';
import assert from 'node:assert/strict';

const root=resolve(dirname(fileURLToPath(import.meta.url)),'..');
const infoFile=(await mkdtemp('/tmp/immoalert-public-'))+'/session.json';
const demo=spawn('python3',[resolve(root,'scripts/serve_member_demo.py'),'--output',infoFile],{cwd:root,stdio:['ignore','ignore','pipe']});
let diagnostics='';demo.stderr.on('data',chunk=>diagnostics+=chunk.toString());
let info=null;
for(let attempt=0;attempt<150&&!info;attempt++){try{info=JSON.parse(await readFile(infoFile,'utf8'))}catch{if(demo.exitCode!==null)throw new Error('Demo failed: '+diagnostics);await new Promise(done=>setTimeout(done,200))}}
if(!info){demo.kill('SIGTERM');throw new Error('Demo startup timed out: '+diagnostics)}
const {chromium}=await browserTools();
const browser=await chromium.launch({headless:true});
try{
 const page=await (await browser.newContext({locale:'fr-FR',viewport:{width:1280,height:900}})).newPage();
 await page.goto(info.url);
 await page.getByRole('heading',{name:'Explorez les annonces.'}).waitFor({timeout:15000});
 const inventory=await page.evaluate(()=>fetch('/api/inventory?limit=5').then(r=>r.json()));
 assert.ok(inventory.items.length>0,'anonymous visitors see listings');
 assert.equal((await page.evaluate(()=>fetch('/api/inventory?favorites=1').then(r=>r.status))),401);
 await page.getByRole('button',{name:/Créer une recherche/}).first().click();
 const dialog=page.getByRole('dialog');await dialog.waitFor();
 await dialog.getByRole('button',{name:/Créer un compte/}).click();
 const login='visiteur'+Date.now();
 await dialog.getByLabel('Identifiant').fill(login);
 await dialog.locator('input[type=password]').fill('Un-mot-de-passe-solide-42');
 await dialog.getByRole('button',{name:/Créer mon compte/}).click();
 let state={account:null};
 for(let attempt=0;attempt<40&&!state.account;attempt++){state=await page.evaluate(()=>fetch('/api/state').then(r=>r.json()));if(!state.account)await page.waitForTimeout(300)}
 assert.equal(state.account?.login,login);
 await page.getByRole('button',{name:'Mon compte'}).first().click();
 await page.getByRole('heading',{name:/Sécurité et partage/}).waitFor({timeout:15000});
 await page.getByText('Notifications push').first().waitFor();
 const shared=await browser.newPage();
 await shared.goto(info.url+'/?share=unknown-token-unknown-token');
 await shared.getByText(/expiré ou révoqué/).waitFor({timeout:10000});
 console.log('Public site check passed.');
}finally{await browser.close();demo.kill('SIGTERM')}
