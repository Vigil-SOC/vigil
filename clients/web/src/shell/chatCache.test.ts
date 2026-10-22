import { beforeEach, describe, expect, it } from 'vitest'
import {
  chatWasImported,
  chatCacheEpoch,
  clearChatCache,
  loadHistory,
  loadKeymap,
  markChatImported,
  retainChatCache,
  saveHistory,
  serverUnreachable,
  setKeymapEntry,
} from './chatCache'

const sample = {
  id: 'c1',
  title: 'Thread',
  ts: 1,
  messages: [{ role: 'vigil', text: 'secret' }],
  key: 'seed',
}

beforeEach(() => localStorage.clear())

describe('chat cache', () => {
  it('stores history and the investigation map under the user id', () => {
    saveHistory('alice', [sample])
    setKeymapEntry('alice', 'seed', 'c1')
    expect(loadHistory('alice')).toEqual([sample])
    expect(loadKeymap('alice')).toEqual({ seed: 'c1' })
    expect(loadHistory('bob')).toEqual([])
    expect(loadKeymap('bob')).toEqual({})
    expect(localStorage.getItem('soc.chat.history')).toBeNull()
    expect(localStorage.getItem('soc.chat.keymap')).toBeNull()
  })

  it('does not read or keep the old unscoped keys', () => {
    localStorage.setItem('soc.chat.history', JSON.stringify([sample]))
    localStorage.setItem('soc.chat.keymap', JSON.stringify({ seed: 'c1' }))
    localStorage.setItem('soc.chat.imported', '1')
    expect(loadHistory('alice')).toEqual([])
    expect(chatWasImported('alice')).toBe(false)
    retainChatCache('alice')
    expect(localStorage.getItem('soc.chat.history')).toBeNull()
    expect(localStorage.getItem('soc.chat.keymap')).toBeNull()
    expect(localStorage.getItem('soc.chat.imported')).toBeNull()
  })

  it('keeps only the signed-in user, and sign-out removes theirs too', () => {
    saveHistory('alice', [sample])
    saveHistory('bob', [{ ...sample, id: 'c2', messages: [{ role: 'user', text: 'bob' }] }])
    markChatImported('bob')
    localStorage.setItem('soc.chat.threadHeight', '400')
    retainChatCache('alice')
    expect(loadHistory('alice')).toEqual([sample])
    expect(loadHistory('bob')).toEqual([])
    expect(chatWasImported('bob')).toBe(false)
    expect(localStorage.getItem('soc.chat.threadHeight')).toBe('400')
    clearChatCache()
    expect(loadHistory('alice')).toEqual([])
    expect(localStorage.getItem('soc.chat.threadHeight')).toBe('400')
  })

  it('bumps an epoch when the cache is narrowed or cleared', () => {
    const before = chatCacheEpoch()
    retainChatCache('alice')
    expect(chatCacheEpoch()).toBe(before + 1)
    clearChatCache()
    expect(chatCacheEpoch()).toBe(before + 2)
  })

  it('treats only a missing response or a 5xx as unreachable', () => {
    expect(serverUnreachable(new Error('Network Error'))).toBe(true)
    expect(serverUnreachable({ response: { status: 500 } })).toBe(true)
    expect(serverUnreachable({ response: { status: 404 } })).toBe(false)
    expect(serverUnreachable({ response: { status: 403 } })).toBe(false)
    expect(serverUnreachable({ response: { status: 401 } })).toBe(false)
  })
})
