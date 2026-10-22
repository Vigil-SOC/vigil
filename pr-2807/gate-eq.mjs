const perms={a:'p'}; let bad=0
for (const key of ['a','b']) for (const has of [true,false]) for (const gate of [undefined,{},{integration:'x'},{integration:'y'}]) {
  const en=['x'], hasPermission=()=>has
  const old=(()=>{const perm=key?perms[key]:undefined; if(perm&&!hasPermission(perm))return false; if(gate?.integration&&!en.includes(gate.integration))return false; return Boolean(key)})()
  const neu=(()=>{const perm=perms[key]; if(perm&&!hasPermission(perm))return false; return !gate?.integration||en.includes(gate.integration)})()
  if(old!==neu){bad++;console.log('DIFF',key,has,gate)}
}
console.log(bad?`FAIL ${bad}`:'old and new nav filter agree on all 16 cases')
