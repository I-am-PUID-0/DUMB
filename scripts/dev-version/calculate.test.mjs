import assert from 'node:assert/strict';
import {test} from 'node:test';
import {
  calculatePlan, candidateFromCommits, createReadOnlyFetch, PlanError,
} from './calculate.mjs';

const head = 'a'.repeat(40);
const stable = 'b'.repeat(40);
const cutoff = 'c'.repeat(40);
const master = 'd'.repeat(40);
const other = 'e'.repeat(40);
const repository = 'example/DUMB';
const token = 'private-fixture-token';
const release = (tag, extra = {}) => ({
  tag_name: tag, draft: false, prerelease: false, ...extra,
});
const commit = (sha, message, pr = undefined) => ({
  sha, message, associatedPullRequests: {nodes: pr ? [pr] : []},
});
const pullRequest = (sha, body) => ({
  number: 10, title: 'Fixture', body, baseRefName: 'dev', headRefName: 'fixture',
  mergeCommit: {oid: sha}, labels: {nodes: []},
  files: {nodes: [], pageInfo: {hasNextPage: false}},
});

function fixture({
  releases = [release('2.23.0')],
  history = [commit(head, 'feat: new feature'), commit(stable, 'chore: release')],
  range = [head],
  cutoffSha = stable,
  tagShas = {'2.23.0': stable},
  localTagShas = tagShas,
  masterAncestors = new Set([stable]),
  shallow = false,
  extraGit,
  response,
} = {}) {
  const requests = [];
  const gitCalls = [];
  const git = async (...args) => {
    gitCalls.push(args);
    if (extraGit) {
      const result = extraGit(args);
      if (result !== undefined) return result;
    }
    if (args[0] === 'rev-parse' && args[1] === '--is-shallow-repository') {
      return String(shallow);
    }
    if (args[0] === 'rev-parse') {
      const ref = args.at(-1);
      if (ref === head + '^{commit}') return head;
      if (ref === 'refs/remotes/origin/master^{commit}') return master;
      const tag = ref.replace('refs/tags/', '').replace('^{commit}', '');
      if (localTagShas[tag]) return localTagShas[tag];
      throw new PlanError('Missing fixture ref.');
    }
    if (args[0] === 'merge-base') {
      return args.at(-1) === master
        ? (masterAncestors.has(args[2]) ? args[2] : cutoff)
        : cutoffSha;
    }
    if (args[0] === 'rev-list') return range.join('\n');
    throw new Error('Unexpected Git fixture call.');
  };
  const fetchImpl = async (input, options = {}) => {
    const url = new URL(input);
    requests.push({url, options});
    assert.equal(options.redirect, 'error');
    if (response) {
      const result = response(url, options);
      if (result) return result;
    }
    if (url.pathname === '/repos/' + repository) {
      return Response.json({default_branch: 'master'});
    }
    if (url.pathname.endsWith('/releases')) return Response.json(releases);
    if (url.pathname.includes('/git/ref/tags/')) {
      const tag = url.pathname.split('/').at(-1);
      return Response.json({ref: 'refs/tags/' + tag,
        object: {type: 'commit', sha: tagShas[tag]}});
    }
    if (url.pathname === '/graphql') {
      const body = JSON.parse(options.body);
      assert.equal(body.variables.targetBranch, head);
      assert.match(body.query, /object\(expression: \$targetBranch\)/);
      assert.doesNotMatch(body.query, /ref\(qualifiedName:/);
      return Response.json({data: {repository: {object: {
        history: {nodes: history, pageInfo: {hasNextPage: false, endCursor: null}},
      }}}});
    }
    throw new Error('Unexpected API fixture request.');
  };
  return {
    requests, gitCalls,
    options: {repository, head, token, git, fetchImpl},
    calculate: options => calculatePlan({repository, head, token, git, fetchImpl, ...options}),
  };
}

test('SDK versioning handles feature, patch, breaking and empty histories', () => {
  const cases = [
    [[], '2.23.1'],
    [['docs: clarify help'], '2.23.1'],
    [['chore: tooling'], '2.23.1'],
    [['not a conventional message'], '2.23.1'],
    [['fix(api): preserve settings'], '2.23.1'],
    [['feat(api): a capability'], '2.24.0'],
    [['feat!: changed contract'], '3.0.0'],
    [['fix: changed contract\n\nBREAKING CHANGE: remove old endpoint'], '3.0.0'],
  ];
  for (const [messages, expected] of cases) {
    assert.equal(candidateFromCommits('2.23.0',
      messages.map((message, index) => ({sha: String(index).repeat(40), message}))), expected);
  }
});

test('SDK preserves squash/nested feature messages and newest Release-As', () => {
  assert.equal(candidateFromCommits('2.23.0', [{
    sha: head, message: 'chore: squash\n\nfeat: new feature',
  }]), '2.24.0');
  assert.equal(candidateFromCommits('2.23.0', [{
    sha: head,
    message: 'chore: mixed changes\n\nBEGIN_NESTED_COMMIT\nfeat!: API change\nEND_NESTED_COMMIT',
  }]), '3.0.0');
  assert.equal(candidateFromCommits('2.23.0', [
    {sha: head, message: 'chore: choose next\n\nRelease-As: 2.30.0'},
    {sha: other, message: 'chore: older choice\n\nRelease-As: 2.29.0'},
  ]), '2.30.0');
});

test('SDK rejects forced releases that do not remain ahead or have prerelease suffixes', () => {
  for (const version of ['2.23.0', '2.22.0', '2.24.0-beta.1']) {
    assert.throws(() => candidateFromCommits('2.23.0', [{
      sha: head, message: 'chore: override\n\nRelease-As: ' + version,
    }]), /stable core newer/);
  }
});

test('actual SDK query is pinned to an immutable object and retains PR body overrides', async () => {
  const f = fixture({history: [
    commit(head, 'fix: misleading squash title', pullRequest(head,
      'Review text\nBEGIN_COMMIT_OVERRIDE\nfeat!: reviewed contract change\nEND_COMMIT_OVERRIDE')),
    commit(stable, 'chore: release'),
  ]});
  const plan = await f.calculate();
  assert.equal(plan.sourceHead, head);
  assert.equal(plan.stableVersion, '2.23.0');
  assert.equal(plan.stableTag, '2.23.0');
  assert.equal(plan.stableSha, stable);
  assert.equal(plan.cutoffSha, stable);
  assert.equal(plan.candidateVersion, '3.0.0');
  assert.equal(plan.coreVersion, '3.0.0');
  assert.equal(plan.displayCount, 1);
  assert.equal(plan.commitCount, 1);
  assert.equal(plan.releasePleaseVersion, '17.6.0');
  assert.equal(JSON.stringify(plan).includes(token), false);
  assert.equal(f.requests.filter(r => r.url.pathname === '/graphql').length, 1);
});

test('release-only master commit uses shared cutoff without replaying released features', async () => {
  const f = fixture({
    releases: [release('2.24.0')],
    tagShas: {'2.24.0': stable},
    cutoffSha: cutoff,
    history: [commit(head, 'chore: dev bookkeeping'), commit(cutoff, 'feat: already released')],
  });
  const plan = await f.calculate();
  assert.equal(plan.stableVersion, '2.24.0');
  assert.equal(plan.cutoffSha, cutoff);
  assert.equal(plan.candidateVersion, '2.24.1');
  assert.equal(plan.displayCount, 1);
});

test('zero commits beyond the shared cutoff still produces the next patch', async () => {
  const f = fixture({cutoffSha: head, range: [], history: [commit(head, 'feat: already released')]});
  const plan = await f.calculate();
  assert.equal(plan.candidateVersion, '2.23.1');
  assert.equal(plan.displayCount, 0);
});

test('latest stable is selected semantically and must be published on master', async () => {
  const f = fixture({
    releases: [
      release('9.0.0', {prerelease: true}), release('8.0.0', {draft: true}),
      release('7.0.0-beta.1'), release('2.24.0'), release('2.9.0'), release('v2.23.0'),
    ],
    tagShas: {'2.24.0': other, 'v2.23.0': stable, '2.9.0': cutoff},
  });
  const plan = await f.calculate();
  assert.equal(plan.stableTag, 'v2.23.0');
  assert.equal(plan.stableVersion, '2.23.0');
  assert.equal(plan.candidateVersion, '2.24.0');
});

test('annotated release tags are API peeled and checked against local commits', async () => {
  const f = fixture({response(url) {
    if (url.pathname.endsWith('/git/ref/tags/2.23.0')) {
      return Response.json({ref: 'refs/tags/2.23.0', object: {type: 'tag', sha: other}});
    }
    if (url.pathname.endsWith('/git/tags/' + other)) {
      return Response.json({object: {type: 'commit', sha: stable}});
    }
  }});
  assert.equal((await f.calculate()).stableSha, stable);
  await assert.rejects(fixture({localTagShas: {'2.23.0': other}}).calculate(),
    /local and published/);
});

test('missing stable release, shallow history and ambiguous merge base fail safely', async () => {
  await assert.rejects(fixture({releases: []}).calculate(), /No published stable/);
  await assert.rejects(fixture({shallow: true}).calculate(), /shallow/);
  await assert.rejects(fixture({cutoffSha: cutoff + '\n' + other}).calculate(), /unique shared/);
});

test('wrong immutable HEAD, truncated range and duplicate history are refused', async () => {
  await assert.rejects(fixture({history: [commit(other, 'feat: wrong'), commit(stable, 'release')]}).calculate(),
    /pinned source head/);
  await assert.rejects(fixture({history: [commit(head, 'feat: partial')]}).calculate(), /truncated/);
  await assert.rejects(fixture({range: [head, other]}).calculate(), /truncated/);
  await assert.rejects(fixture({history: [
    commit(head, 'feat: repeated'), commit(head, 'feat: repeated'), commit(stable, 'release'),
  ]}).calculate(), /repeated a commit/);
});

test('independent history and release bounds cannot silently truncate a page', async () => {
  await assert.rejects(fixture({range: [head, other]}).calculate({maxCommits: 1}), /explicit bound/);
  await assert.rejects(fixture({history: [
    commit(head, 'feat: one'), commit(other, 'fix: two'), commit(stable, 'release'),
  ]}).calculate({maxCommits: 1}), /scan bound/);
  await assert.rejects(fixture({releases: [release('2.23.0'), release('2.22.0')]}).calculate({
    maxReleases: 1,
  }), /explicit bound/);
});

test('Git range filtering ignores unrelated older SDK history while requiring every range commit', async () => {
  const f = fixture({range: [head, other], cutoffSha: cutoff, history: [
    commit(head, 'fix: current'), commit(stable, 'feat!: unrelated old history'),
    commit(cutoff, 'release'), commit(other, 'feat: older-dated development work'),
  ]});
  assert.equal((await f.calculate()).candidateVersion, '2.24.0');
});

test('read-only API transport rejects writes, other origins and unpinned SDK query shapes', async () => {
  let networkCalls = 0;
  const readFetch = createReadOnlyFetch({
    repository, head, fetchImpl: async () => { networkCalls++; return Response.json({}); },
  });
  for (const [url, options] of [
    ['https://evil.example/graphql', {method: 'POST'}],
    ['https://api.github.com/repos/example/elsewhere', {}],
    ['https://api.github.com/repos/example/DUMB/releases', {method: 'POST'}],
    ['https://api.github.com/graphql', {method: 'POST', body: JSON.stringify({query: 'mutation { x }'})}],
    ['https://api.github.com/graphql', {method: 'POST', body: JSON.stringify({query: 'query other { x }'})}],
  ]) {
    await assert.rejects(readFetch(url, options), PlanError);
  }
  assert.equal(networkCalls, 0);
});

test('API error details and credentials never enter public diagnostics', async () => {
  const f = fixture({response() {
    return Response.json({message: token + ' private response'}, {status: 403});
  }});
  await assert.rejects(f.calculate(), error =>
    error instanceof PlanError && /HTTP 403/.test(error.message)
      && !error.message.includes(token) && !error.message.includes('private response'));
});
