import { describe, expect, it } from 'vitest'
import { urlIsPrivateHost } from './loopback'

describe('urlIsPrivateHost', () => {
  it.each([
    'http://localhost:8000',
    'http://127.0.0.1',
    'http://[::1]:8000/v1',
    'http://10.1.2.3',
    'http://172.16.0.1',
    'http://172.31.255.1',
    'http://192.168.1.20:11434',
    'http://169.254.1.1',
    'http://[fe80::1]:8000',
    'http://host.docker.internal:9000',
    'http://llm:8000',
    'http://box.local',
    'http://vllm.internal:8000/v1/models',
    'http://LLM.Internal.:8000',
  ])('treats %s as private', (url) => expect(urlIsPrivateHost(url)).toBe(true))

  it.each([
    'http://172.15.0.1',
    'http://172.32.0.1',
    'http://8.8.8.8',
    'https://api.openai.com',
    'https://localhost.example.com',
    'http://[2001:db8::1]:8000',
    'not a url',
    '',
  ])('treats %j as not private', (url) => expect(urlIsPrivateHost(url)).toBe(false))
})
