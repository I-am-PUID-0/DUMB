#!/usr/bin/env node
/** Read-only development-version planning. The caller owns publishing. */
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {GitHub, VERSION, setLogger} from 'release-please';
import {parseConventionalCommits} from 'release-please/build/src/commit.js';
import {Version} from 'release-please/build/src/version.js';
import {DefaultVersioningStrategy} from 'release-please/build/src/versioning-strategies/default.js';

const exec = promisify(execFile);
const SHA = /^[0-9a-f]{40}$/;
const REPOSITORY = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
const STABLE = /^(?:v)?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/;
const quiet = Object.fromEntries(
  ['debug', 'info', 'warn', 'error', 'trace'].map(name => [name, () => {}]),
);
setLogger(quiet);
export class PlanError extends Error {}

function requireSha(value, label) {
  if (typeof value !== 'string' || !SHA.test(value)) {
    throw new PlanError(label + ' must be an immutable full Git commit SHA.');
  }
  return value;
}
function stableCore(value) {
  if (typeof value !== 'string' || value.length > 100 || !STABLE.test(value)) return null;
  const core = value.replace(/^v/, '');
  if (core.split('.').some(part => !Number.isSafeInteger(Number(part)))) return null;
  return core;
}
function positiveBound(value, maximum, label) {
  if (!Number.isSafeInteger(value) || value < 1 || value > maximum) {
    throw new PlanError(label + ' is outside its supported bound.');
  }
  return value;
}

/**
 * Preserve the pinned SDK's PR/commit normalization, changing only its moving
 * ref query to an immutable object and adapting the response envelope.
 */
export function createReadOnlyFetch({repository, head, fetchImpl = fetch}) {
  if (!REPOSITORY.test(repository)) throw new PlanError('Invalid repository.');
  requireSha(head, 'Source head');
  const root = '/repos/' + repository;
  return async (input, options = {}) => {
    const url = new URL(typeof input === 'string' ? input : input.url);
    const method = (options.method || 'GET').toUpperCase();
    if (url.origin !== 'https://api.github.com' || url.username || url.password) {
      throw new PlanError('Only the selected GitHub API origin is permitted.');
    }
    let pinned = false;
    const request = {...options, redirect: 'error'};
    if (method === 'GET') {
      const allowed = url.pathname === root
        || url.pathname === root + '/releases'
        || new RegExp('^' + root.replaceAll('.', '\\.')
          + '/git/(ref/tags/[^/]+|tags/[0-9a-f]{40})$').test(url.pathname);
      if (!allowed) throw new PlanError('Unsupported read-only repository endpoint.');
      if (url.pathname !== root + '/releases' && url.search) {
        throw new PlanError('Unexpected repository endpoint parameters.');
      }
      if (url.pathname === root + '/releases') {
        const parameters = [...url.searchParams.keys()].sort().join(',');
        if (parameters !== 'page,per_page'
          || url.searchParams.get('per_page') !== '100'
          || !/^[1-9]\d*$/.test(url.searchParams.get('page') || '')) {
          throw new PlanError('Invalid release pagination.');
        }
      }
    } else if (method === 'POST' && url.pathname === '/graphql' && !url.search) {
      let body;
      try { body = JSON.parse(options.body); }
      catch { throw new PlanError('The pinned SDK GraphQL request is unreadable.'); }
      const query = body.query;
      const fromRef = 'ref(qualifiedName: $targetBranch) {';
      const fromTarget = 'target {';
      if (typeof query !== 'string' || !/^\s*query pullRequestsSince\(/.test(query)
        || /\bmutation\b/.test(query) || body.variables?.targetBranch !== head
        || body.variables?.owner !== repository.split('/')[0]
        || body.variables?.repo !== repository.split('/')[1]
        || query.split(fromRef).length !== 2 || query.split(fromTarget).length !== 2) {
        throw new PlanError('The pinned SDK history query shape changed.');
      }
      body.query = query.replace(fromRef, 'object(expression: $targetBranch) {')
        .replace(fromTarget, '... on Commit {');
      request.body = JSON.stringify(body);
      pinned = true;
    } else {
      throw new PlanError('GitHub writes and unsupported API requests are forbidden.');
    }
    let response;
    try {
      response = await fetchImpl(url.href, {
        ...request, signal: options.signal || AbortSignal.timeout(30_000),
      });
    } catch { throw new PlanError('A read-only GitHub request failed.'); }
    if (!response.ok) {
      throw new PlanError('A read-only GitHub request failed (HTTP ' + response.status + ').');
    }
    if (!pinned) return response;
    let body;
    try { body = await response.json(); }
    catch { throw new PlanError('The pinned SDK history response is unreadable.'); }
    if (body.errors || !body.data?.repository?.object?.history) {
      throw new PlanError('Immutable GitHub history is unavailable.');
    }
    body.data.repository.ref = {target: body.data.repository.object};
    delete body.data.repository.object;
    return Response.json(body);
  };
}

export function makeGit(repoDir) {
  return async (...arguments_) => {
    try {
      const result = await exec('git', ['-C', repoDir, ...arguments_], {
        maxBuffer: 8 * 1024 * 1024, timeout: 30_000,
        env: {...process.env, GIT_OPTIONAL_LOCKS: '0', GIT_TERMINAL_PROMPT: '0'},
      });
      return result.stdout.trim();
    } catch {
      throw new PlanError('Read-only Git ' + arguments_[0]
        + ' failed; fetch complete history and tags.');
    }
  };
}
async function jsonRequest(readFetch, path, token) {
  try {
    const response = await readFetch('https://api.github.com' + path, {
      method: 'GET',
      headers: {authorization: 'Bearer ' + token, accept: 'application/vnd.github+json',
        'x-github-api-version': '2022-11-28'},
    });
    return await response.json();
  } catch (error) {
    if (error instanceof PlanError) throw error;
    throw new PlanError('A GitHub read response was invalid.');
  }
}
async function resolvePublishedTag({repository, tag, token, readFetch, git}) {
  let value = await jsonRequest(readFetch,
    '/repos/' + repository + '/git/ref/tags/' + encodeURIComponent(tag), token);
  if (value.ref !== 'refs/tags/' + tag) {
    throw new PlanError('The published release tag did not resolve exactly.');
  }
  let object = value.object;
  const seen = new Set();
  for (let depth = 0; depth < 8; depth += 1) {
    requireSha(object?.sha, 'Published tag object');
    if (object.type === 'commit') {
      const local = await git('rev-parse', '--verify', '--end-of-options',
        'refs/tags/' + tag + '^{commit}');
      if (local !== object.sha) {
        throw new PlanError('The local and published release tag commits differ.');
      }
      return object.sha;
    }
    if (object.type !== 'tag' || seen.has(object.sha)) break;
    seen.add(object.sha);
    value = await jsonRequest(readFetch,
      '/repos/' + repository + '/git/tags/' + object.sha, token);
    object = value.object;
  }
  throw new PlanError('The published release tag cannot be peeled safely.');
}
async function latestStable({repository, token, readFetch, git, masterSha, maxReleases}) {
  const releases = [];
  for (let page = 1; ; page += 1) {
    const rows = await jsonRequest(readFetch,
      '/repos/' + repository + '/releases?per_page=100&page=' + page, token);
    if (!Array.isArray(rows) || rows.length > 100) {
      throw new PlanError('The published release list is invalid.');
    }
    if (releases.length + rows.length > maxReleases) {
      throw new PlanError('Published release history exceeds the explicit bound.');
    }
    releases.push(...rows);
    if (rows.length < 100) break;
  }
  const candidates = releases
    .filter(release => release.draft === false && release.prerelease === false
      && stableCore(release.tag_name))
    .map(release => ({tag: release.tag_name, version: stableCore(release.tag_name)}))
    .sort((a, b) => Version.parse(b.version).compare(Version.parse(a.version)));
  for (const candidate of candidates) {
    const sha = await resolvePublishedTag({
      repository, tag: candidate.tag, token, readFetch, git,
    });
    const ancestors = await git('merge-base', '--all', sha, masterSha);
    if (ancestors.split('\n').includes(sha)) return {...candidate, sha};
  }
  throw new PlanError('No published stable release reachable from master was found.');
}
export function candidateFromCommits(stableVersion, commits) {
  const parsed = parseConventionalCommits(commits, quiet);
  const baseline = Version.parse(stableVersion);
  const version = new DefaultVersioningStrategy({logger: quiet}).bump(baseline, parsed);
  const candidate = version.toString();
  if (stableCore(candidate) !== candidate || version.compare(baseline) <= 0) {
    throw new PlanError('The calculated source version must be a stable core newer than the release.');
  }
  return candidate;
}
export async function calculatePlan({
  repository, head, token, repoDir = process.cwd(),
  masterRef = 'refs/remotes/origin/master', maxCommits = 20_000,
  maxReleases = 1_000, fetchImpl = fetch, git = makeGit(repoDir),
  githubFactory = options => GitHub.create(options),
}) {
  if (!REPOSITORY.test(repository || '')) throw new PlanError('Invalid repository.');
  requireSha(head, 'Source head');
  if (typeof token !== 'string' || !token) throw new PlanError('A GitHub read token is required.');
  if (typeof masterRef !== 'string' || !masterRef || masterRef.startsWith('-')) {
    throw new PlanError('A valid master ref is required.');
  }
  positiveBound(maxCommits, 100_000, 'Commit scan limit');
  positiveBound(maxReleases, 10_000, 'Release scan limit');
  if (await git('rev-parse', '--is-shallow-repository') !== 'false') {
    throw new PlanError('Complete Git history is required; shallow clones are unsupported.');
  }
  if (await git('rev-parse', '--verify', '--end-of-options', head + '^{commit}') !== head) {
    throw new PlanError('The pinned source head is unavailable.');
  }
  const masterSha = requireSha(await git('rev-parse', '--verify', '--end-of-options',
    masterRef + '^{commit}'), 'Master head');
  const readFetch = createReadOnlyFetch({repository, head, fetchImpl});
  const stable = await latestStable({
    repository, token, readFetch, git, masterSha, maxReleases,
  });
  const bases = (await git('merge-base', '--all', stable.sha, head)).split('\n');
  if (bases.length !== 1 || !SHA.test(bases[0])) {
    throw new PlanError('A unique shared release/development history cutoff is required.');
  }
  const cutoffSha = bases[0];
  const range = await git('rev-list', '--max-count=' + (maxCommits + 1),
    cutoffSha + '..' + head);
  const expected = new Set(range ? range.split('\n') : []);
  if (expected.size > maxCommits || [...expected].some(sha => !SHA.test(sha))) {
    throw new PlanError('Development commit history exceeds the explicit bound.');
  }
  const github = await githubFactory({
    owner: repository.split('/')[0], repo: repository.split('/')[1],
    defaultBranch: 'dev', token, fetch: readFetch, logger: quiet,
  });
  const commits = [];
  const scanned = new Set();
  const remaining = new Set(expected);
  let reachedCutoff = false;
  for await (const commit of github.mergeCommitIterator(head, {
    maxResults: maxCommits + 1, batchSize: 50, backfillFiles: false,
  })) {
    requireSha(commit.sha, 'History commit');
    if (!scanned.size && commit.sha !== head) {
      throw new PlanError('GitHub history did not start at the pinned source head.');
    }
    if (scanned.has(commit.sha)) throw new PlanError('GitHub history repeated a commit.');
    scanned.add(commit.sha);
    if (scanned.size > maxCommits + 1) {
      throw new PlanError('GitHub history exceeds the explicit scan bound.');
    }
    if (commit.sha === cutoffSha) reachedCutoff = true;
    if (remaining.delete(commit.sha)) commits.push(commit);
    if (reachedCutoff && remaining.size === 0) break;
  }
  if (!reachedCutoff || remaining.size) {
    throw new PlanError('GitHub history was truncated before the complete cutoff range.');
  }
  const candidateVersion = candidateFromCommits(stable.version, commits);
  return {
    sourceHead: head, stableVersion: stable.version, stableTag: stable.tag,
    stableSha: stable.sha, masterSha, cutoffSha, candidateVersion,
    coreVersion: candidateVersion, displayCount: expected.size,
    commitCount: commits.length, releasePleaseVersion: VERSION,
  };
}
export async function main(arguments_ = process.argv.slice(2)) {
  const options = {};
  const names = new Map([
    ['--repository', 'repository'], ['--head', 'head'], ['--repo-dir', 'repoDir'],
    ['--master-ref', 'masterRef'], ['--max-commits', 'maxCommits'],
    ['--max-releases', 'maxReleases'],
  ]);
  for (let index = 0; index < arguments_.length; index += 2) {
    const key = names.get(arguments_[index]);
    const value = arguments_[index + 1];
    if (!key || value === undefined || value.startsWith('--') || key in options) {
      throw new PlanError('Expected unique supported --option value pairs.');
    }
    options[key] = key.startsWith('max') ? Number(value) : value;
  }
  const plan = await calculatePlan({
    ...options, token: process.env.GH_TOKEN || process.env.GITHUB_TOKEN,
  });
  process.stdout.write(JSON.stringify(plan) + '\n');
}
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch(error => {
    const message = error instanceof PlanError ? error.message : 'Version planning failed safely.';
    process.stderr.write(message + '\n');
    process.exitCode = 1;
  });
}
