import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import UsersSection from './UsersSection'
import type { Role, RolesMeta, User } from './useSettings'

const users = vi.hoisted(() => ({
  users: [] as User[],
  roles: [] as Role[],
  rolesMeta: {
    presets: [],
    permissions: [],
    platform_equivalent: [],
    nobody_can_manage_roles: false,
  } as RolesMeta,
  phase: 'ready' as 'loading' | 'ready' | 'error',
  error: null as string | null,
  reload: vi.fn(),
  createUser: vi.fn(),
  updateUser: vi.fn(),
  deleteUser: vi.fn(),
  createRole: vi.fn(),
  updateRole: vi.fn(),
  deleteRole: vi.fn(),
}))

vi.mock('./useSettings', () => ({
  useUsers: () => users,
}))

vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({ hasPermission: () => true }),
}))

const ROLE: Role = {
  role_id: 'role-admin',
  name: 'Admin',
  description: 'Full access',
  permissions: { 'users.write': true },
  is_system_role: true,
  kind: 'builtin',
  unrecognised: [],
  holders: 1,
}

const notify = vi.fn()

function renderSection() {
  return render(<UsersSection notify={notify} />)
}

function openCreate() {
  renderSection()
  fireEvent.click(screen.getByRole('button', { name: /Add User/ }))
}

const input = (name: RegExp) => screen.getByLabelText(name, { selector: 'input' })

function fillAccount(password: string) {
  fireEvent.change(input(/^Username/), { target: { value: 'ada' } })
  fireEvent.change(input(/^Full Name/), { target: { value: 'Ada Lovelace' } })
  fireEvent.change(input(/^Email/), { target: { value: 'ada@example.com' } })
  fireEvent.change(input(/^Password/), { target: { value: password } })
}

beforeEach(() => {
  vi.clearAllMocks()
  users.users = []
  users.roles = [ROLE]
  users.phase = 'ready'
  users.error = null
  users.createUser.mockResolvedValue(undefined)
})

describe('UsersSection create user', () => {
  it('sends a 10-character password instead of refusing it client-side', async () => {
    openCreate()
    fillAccount('ten-chars!')
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(users.createUser).toHaveBeenCalledWith({
      username: 'ada',
      email: 'ada@example.com',
      password: 'ten-chars!',
      full_name: 'Ada Lovelace',
      role_id: 'role-admin',
    }))
    expect(screen.queryByText(/at least 8/i)).not.toBeInTheDocument()
    await waitFor(() => expect(notify).toHaveBeenCalledWith('ok', 'Created ada.'))
  })

  it("shows the server's 400 policy message in the dialog", async () => {
    users.createUser.mockRejectedValue({
      response: { status: 400, data: { detail: 'Password must be at least 12 characters long' } },
    })
    openCreate()
    fillAccount('ten-chars!')
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    expect(await screen.findByText('Password must be at least 12 characters long')).toBeInTheDocument()
    expect(screen.getByRole('dialog', { name: 'Create New User' })).toBeInTheDocument()
  })

  it('hints at 12 characters, not 8', () => {
    openCreate()
    expect(screen.getByText('At least 12 characters. A few unrelated words make a strong one.')).toBeInTheDocument()
    expect(screen.queryByText(/8 character/i)).not.toBeInTheDocument()
  })
})

describe('UsersSection roles', () => {
  beforeEach(() => {
    users.rolesMeta = {
      presets: [
        {
          key: 'auditor',
          name: 'Auditor',
          description: 'Reads everything.',
          permissions: { 'audit.read': true, 'users.read': true },
        },
      ],
      permissions: ['audit.read', 'users.read', 'users.write'],
      platform_equivalent: [],
      nobody_can_manage_roles: false,
    }
  })

  it('copies a preset into a new custom role', async () => {
    users.createRole.mockResolvedValue(undefined)
    renderSection()
    fireEvent.click(screen.getByRole('button', { name: /New Role/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Start from…' }))
    fireEvent.click(screen.getByRole('option', { name: 'Auditor (preset)' }))
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    await waitFor(() =>
      expect(users.createRole).toHaveBeenCalledWith({
        name: 'Auditor',
        description: 'Reads everything.',
        permissions: { 'audit.read': true, 'users.read': true },
      }),
    )
  })

  it("shows the server's refusal, naming the permission", async () => {
    users.createRole.mockRejectedValue({
      response: { status: 403, data: { detail: "Cannot create this role: you don't hold audit.read" } },
    })
    renderSection()
    fireEvent.click(screen.getByRole('button', { name: /New Role/ }))
    fireEvent.change(screen.getByLabelText(/^Name/, { selector: 'input' }), { target: { value: 'Trail' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    expect(await screen.findByText("Cannot create this role: you don't hold audit.read")).toBeInTheDocument()
  })

  it('opens a built-in read-only, offering a copy', () => {
    renderSection()
    fireEvent.click(screen.getByRole('button', { name: 'View' }))
    expect(screen.getByText(/Built-in roles can’t be changed/)).toBeInTheDocument()
    expect(within(screen.getByRole('dialog')).getByRole('button', { name: 'Copy' })).toBeInTheDocument()
  })
})
