// The tile beside an integration's name (PRD D6): its vendor logo on a white
// tile, as the boards draw source logos, or the neutral category icon.
import { Icon } from '../../shared/icons'
import { categoryIcon } from './integrationsData'

export default function IntegrationMark({ logo, category }: { logo?: string; category: string }) {
  return (
    <span className={`int-mark${logo ? ' logo' : ''}`} aria-hidden="true">
      {logo ? <img src={logo} alt="" /> : <Icon name={categoryIcon(category)} size={15} />}
    </span>
  )
}
