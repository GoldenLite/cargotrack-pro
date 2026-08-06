<?xml version="1.0" encoding="utf-8"?>
<!-- ExpressRegistryKindCode='ДТЭГ' - Решение Коллегии ЕЭК от 28.08.2018 № 142
ExpressRegistryKindCode='ПДТЭГ' - Решение Коллегии ЕЭК от 16.10.2018 № 158 -->
<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform" 
	xmlns:ecd="urn:customs.ru:Information:CustomsDocuments:ExpressCargoDeclaration:5.27.0" 
	xmlns:xs="http://www.w3.org/2001/XMLSchema" 
	xmlns:cat_ru="urn:customs.ru:CommonAggregateTypes:5.24.0" 
	xmlns:clt_ru="urn:customs.ru:CommonLeafTypes:5.10.0" 
	xmlns:RUSclt_ru="urn:customs.ru:RUSCommonLeafTypes:5.21.0" 
	xmlns:RUScat_ru="urn:customs.ru:RUSCommonAggregateTypes:5.24.0" 
	xmlns:RUDECLcat="urn:customs.ru:RUDeclCommonAggregateTypesCust:5.27.0" 
	xmlns:cltESAD_cu="urn:customs.ru:CUESADCommonLeafTypes:5.17.0" 
	xmlns:catESAD_cu="urn:customs.ru:CUESADCommonAggregateTypesCust:5.27.0"
	xmlns:ecdcm="urn:customs.ru:Information:CustomsDocuments:ExpressCargoDeclarationCustomMark:5.16.0">
	<xsl:output method="html" encoding="utf-8" indent="yes" />	
	<xsl:variable name="cnt" select="0"/>
	<xsl:param name="ExpressCargoDeclarationCustomMark"/>
	<!-- Шаблон для типа ExpressCargoDeclarationType -->
	<xsl:template match="ecd:ExpressCargoDeclaration">
		<xsl:param name="w" select="277"/>	
		<xsl:text disable-output-escaping='yes'>&lt;!DOCTYPE html&gt;</xsl:text>		
		<html>
			<head>
				<style>
                  body {
                  background: #ffffff;
                  font-family: Arial;
                  }

                  div
                  {
                  white-space: normal;
                  }
/*
                  div.page {
                  margin: 10px auto;
                  margin-top: 6pt;
                  margin-bottom: 6pt;
                  padding: 5mm 10mm 10mm 2mm;
                  background: #ffffff;
                  border: solid 1pt #000000;
                  }
*/
                  .marg-top
                  {
                  margin-top:5mm;
                  }

                  table
                  {
                  width: 100%;
                  border: 0;
                  empty-cells: show;
                  border-collapse: collapse;
                  /*margin-top: 1px;*/                  
                  margin: 0px;
                  }

                  table.border tr td
                  {
                  border: 1px solid windowtext;
                  }

                  .value
                  {
                  border-bottom: solid 1px black;
                  font-family: Arial;
                  font-size: 11pt;
                  font-style: italic;
                  }

                  .annot
                  {
                  font-family: Arial;
                  font-size: 11pt;
                  }


                  .title
                  {
                  font-weight:bold;
                  font-family: Arial;
                  font-size: 11pt;
                  }

                  tr.title td
                  {
                  font-weight:bold;
                  font-family: Arial;
                  font-size: 9pt;
                  }
				  .main_title
				  {
				  font-weight:bold;
                  font-family: Arial;
                  font-size: 10pt;
				  }	


                  .bordered { border-collapse: collapse; }
                  td.bordered
                  {
                  border: solid 1px windowtext;
                  }
                  td.bordered_fat2
                  {
                  border-top: solid 3px windowtext;
                  border-bottom: solid 3px windowtext;
                  border-right: solid 1px windowtext;
                  border-left: solid 1px windowtext;
                  }
                  td.bordered_fat3
                  {
                  border-top: solid 4px windowtext;
                  border-bottom: solid 4px windowtext;
                  border-right: solid 1px windowtext;
                  border-left: solid 1px windowtext;
                  }

                  td.graphMain
                  {
                  vertical-align:top;
                  }
                  td.value.graphMain
                  {
                  vertical-align:bottom;
                  }                  
				  span 
				  {
				  font-weight:normal;
                  font-family: Arial;
                  font-size: 9pt;
				  }	                  
                </style>
			</head>
			<body>
				<div class="page" >
					<xsl:if test="ecd:DocType = '0'">
						<xsl:if test="ecd:ExpressRegistryKindCode = 'ДТЭГ'">
						<table cellpadding="4" >
							<tbody>
								<tr>
									<td/>
									<td/>
									<td/>
									<td/>
									<td/>
									<td/>
									<td/>
									<td/>
									<td/>
									<td/>
									<td/>
									<td/>
									<td/>
									<td/>
									<td/>
									<td/>
								</tr>
								<tr>
									<td colspan="16" style="border-top:1px solid black;border-left:1px solid black; border-right:1px solid black;" align="center" valign="middle"><b>Декларация на товары для экспресс-грузов</b></td>
								</tr>
								<tr valign="top">
									<td class="bordered main_title" colspan="4" rowspan="2">
										Отправитель (по общей накладной)<br/>
										<span><xsl:apply-templates mode="org" select="ecd:GoodsShipment/ecd:ConsignorDetails"/></span>
									</td>
									<td class="bordered main_title" colspan="3" rowspan="2">Получатель (по общей накладной)<br/>
										<span><xsl:apply-templates mode="org" select="ecd:GoodsShipment/ecd:ConsigneeDetails"/></span>	
									</td>
									<td colspan="5" class="bordered main_title">A<br/>
										<span style="font-size:14pt;">
											<xsl:value-of select="ecd:RegNUM"/>
										</span>											
									</td>
									<td colspan="2" class="bordered main_title">ДТЭГ<br/>
										<span>
											<xsl:value-of select="ecd:ElectronicDocumentSign"/>
										</span>										
									</td>
									<td colspan="2" class="bordered main_title">Особенность<br/>
										<span>
											<xsl:value-of select="ecd:DeclarationKind"></xsl:value-of>
										</span>
									</td>
								</tr>
								<tr valign="top">
									<td colspan="5" class="bordered main_title">Предшествующий документ<br/>
										<span>
											<xsl:call-template name="PrecedingDocDetails_info"/>
										</span>
									</td>
									<td colspan="2" class="bordered main_title">Кол-во листов 
										<span><xsl:value-of select="ecd:TotalSheetNumber"/></span>										
									</td>
									<td colspan="2" class="bordered main_title">Процедура<br/>
										<span>
											<xsl:value-of select="ecd:CustomsModeCode"/>
											<xsl:text> </xsl:text>
											<xsl:value-of select="ecd:PreviousCustomsModeCode"/>
										</span>										
									</td>
								</tr>
								<tr align="center" valign="top" style="font-size: 10pt;font-weight:bold;">
									<td class="bordered" colspan="5">Общие сведения</td>
									<td class="bordered" colspan="8">Сведения о товарах</td>
									<td class="bordered" colspan="2">Сведения о документах</td>
									<td class="bordered" >Примечание</td>
								</tr>
								<tr align="center" valign="top" style="font-size: 10pt;">
									<td class="bordered" rowspan="2">N п/п</td>
									<td class="bordered" rowspan="2">Общая накладная</td>
									<td class="bordered" rowspan="2">Инд. накладная</td>
									<td class="bordered" rowspan="2">Инд. отправитель<sup>1</sup></td>
									<td class="bordered" rowspan="2">Инд. получатель<sup>2</sup></td>
									<td class="bordered" rowspan="2">N п/п</td>
									<td class="bordered" rowspan="2">Наименование</td>
									<td class="bordered" rowspan="2">Код ТН ВЭД ЕАЭС</td>
									<td class="bordered" rowspan="2">Кол-во</td>
									<td class="bordered" colspan="2">Вес</td>
									<td class="bordered" rowspan="2">Валюта, стоимость</td>
									<td class="bordered" rowspan="2">Таможенная стоимость</td>
									<td class="bordered" rowspan="2">Код, признак</td>
									<td class="bordered" style="font-size: 10pt;" rowspan="2">Дата, номер</td>
									<td class="bordered" rowspan="3"/>
								</tr>
								<tr align="center" valign="top" style="font-size: 10pt;">
									<td class="bordered">брутто</td>
									<td class="bordered">нетто</td>
								</tr>
								<tr align="center" style="font-style:italic;font-size: 10pt;">
									<td class="bordered">1</td>
									<td class="bordered">2</td>
									<td class="bordered">3</td>
									<td class="bordered">4</td>
									<td class="bordered">5</td>
									<td class="bordered">6</td>
									<td class="bordered">7</td>
									<td class="bordered">8</td>
									<td class="bordered">9</td>
									<td class="bordered">10</td>
									<td class="bordered">11</td>
									<td class="bordered">12</td>
									<td class="bordered">13</td>
									<td class="bordered">14</td>
									<td class="bordered">15</td>
								</tr>																
								<xsl:for-each select="ecd:GoodsShipment/ecd:HouseShipment">
									<xsl:variable name="tov" select="count(ecd:GoodsItemDetails)"/>
									<xsl:variable name="rowspan" select="count(ecd:GoodsItemDetails[not(ecd:PresentedDocDetails)])+count(ecd:GoodsItemDetails/ecd:PresentedDocDetails)"/>
									<tr align="center" valign="top" style="font-size: 9pt;">
										<td class="bordered">
											<xsl:if test="$rowspan &gt; 0">
												<xsl:attribute name="rowspan"><xsl:value-of select="$rowspan+1"/></xsl:attribute>
											</xsl:if>
											<xsl:value-of select="ecd:ObjectOrdinal"/>
										</td>
										<td class="bordered">
											<xsl:if test="$rowspan &gt; 1">
												<xsl:attribute name="rowspan"><xsl:value-of select="$rowspan"/></xsl:attribute>
											</xsl:if>
											<xsl:apply-templates mode="doc" select="ecd:TransportDocumentDetails"/>
										</td>
										<td class="bordered">
											<xsl:if test="$rowspan &gt; 1">
												<xsl:attribute name="rowspan"><xsl:value-of select="$rowspan"/></xsl:attribute>
											</xsl:if>
											<xsl:apply-templates mode="doc" select="ecd:HouseWaybillDetails"/>
										</td>
										<td class="bordered">
											<xsl:if test="$rowspan &gt; 1">
												<xsl:attribute name="rowspan"><xsl:value-of select="$rowspan"/></xsl:attribute>
											</xsl:if>
											<xsl:apply-templates mode="org" select="ecd:ConsignorDetails"/>
										</td>
										<td class="bordered">
											<xsl:if test="$rowspan &gt; 1">
												<xsl:attribute name="rowspan"><xsl:value-of select="$rowspan"/></xsl:attribute>
											</xsl:if>
											<xsl:apply-templates mode="org" select="ecd:ConsigneeDetails"/>
										</td>
										<xsl:if test="not(ecd:GoodsItemDetails)">
											<td class="bordered"/>
											<td class="bordered"/>
											<td class="bordered"/>
											<td class="bordered"/>
											<td class="bordered"/>
											<td class="bordered"/>
											<td class="bordered"/>
											<td class="bordered"/>
											<td class="bordered"/>
											<td class="bordered"/>
											<td class="bordered"/>
										</xsl:if>
										<xsl:apply-templates select="ecd:GoodsItemDetails[1]" mode="dt"/>
									</tr>
									<xsl:for-each select="ecd:GoodsItemDetails[position() = 1]">
										<xsl:for-each select="ecd:PresentedDocDetails[position() &gt; 1]">
											<tr align="center" valign="top" style="font-size: 9pt;">												
												<xsl:apply-templates select="."/>
											</tr>
										</xsl:for-each>
									</xsl:for-each>
									<xsl:for-each select="ecd:GoodsItemDetails[position() &gt; 1]">
										<tr align="center" valign="top" style="font-size: 9pt;">
											<xsl:apply-templates select="." mode="dt"/>
										</tr>
										<xsl:for-each select="ecd:PresentedDocDetails[position() &gt; 1]">
											<tr align="center" valign="top" style="font-size: 9pt;">
												<xsl:apply-templates select="."/>
											</tr>
										</xsl:for-each>
									</xsl:for-each>
									
									<tr align="center" valign="top" style="font-size: 10pt;">				
										<td class="bordered_fat2" colspan="8" align="left">
											Всего по индивидуальной накладной<br/>
											<span style="font-style:italic;font-size: 8pt;">
												(общий вес брутто, таможенная стоимость)
											</span>
										</td>										
										<!--<xsl:if test="$tov = 1">
											<xsl:variable name="c_value" select="ecd:GoodsItemDetails/ecd:CAValueAmount[2]"/>
											<!-\- если один товар -\->
											<xsl:choose>
												<xsl:when test="$c_value">
													<xsl:text> </xsl:text>
												</xsl:when>
												<xsl:otherwise>
													<td  class="bordered_fat2" />
												</xsl:otherwise>
											</xsl:choose>	
										</xsl:if>-->										
										<td class="bordered_fat2" >
											<xsl:apply-templates mode="quantity" select="ecd:UnifiedGrossWeightQuantity"/>
										</td>
										<td  class="bordered_fat2" />										
										<td  class="bordered_fat2" />										
										<td class="bordered_fat2">
											<xsl:apply-templates mode="pricevalue" select="ecd:CustomsCost"/>
										</td>
										<td  class="bordered_fat2" colspan="3" style="font-style:italic;font-size: 8pt;">
											<xsl:value-of select="ecd:Design"/>
											<xsl:text> </xsl:text>
											<xsl:value-of select="ecd:TaxBase_DecisionDate"/>
										</td>										
									</tr>
									
								</xsl:for-each>
								<tr align="center" valign="top" style="font-size: 10pt;">
									<td class="bordered_fat3" style="font-weight:bold;" colspan="9" align="left">
											Всего по декларации на товары для экспресс-грузов<br/>
										<span style="font-style:italic;font-size: 8pt;">
											(общий вес брутто, таможенная стоимость)
										</span>
										</td>
									<td class="bordered_fat3" style="font-weight:bold;">
										<xsl:apply-templates mode="quantity" select="ecd:GoodsShipment/ecd:UnifiedGrossWeightQuantity"/>
									</td>
									<td class="bordered_fat3" colspan="2"/>
									<td class="bordered_fat3" style="font-weight:bold;">
										<xsl:apply-templates mode="pricevalue" select="ecd:GoodsShipment/ecd:CustomsCost"/>
									</td>
									<td class="bordered_fat3" colspan="3"/>
								</tr>
							</tbody>
						</table>
						<br/>
						<br/>
						<table cellpadding="4">
							<tbody>
								<xsl:variable name="cnt_GoodsItemDetails" select="count(ecd:GoodsShipment/ecd:HouseShipment/ecd:GoodsItemDetails/ecd:CustomsPaymentDetails)+count(ecd:GoodsShipment/ecd:HouseShipment)+4"/>
								<xsl:variable name="row_part1" select="number($cnt_GoodsItemDetails)-number(2)"/>
								<tr align="center" valign="top">
									<td class="bordered main_title" colspan="6">B. Исчисление платежей</td>
									<td class="bordered main_title" align="left" rowspan="{$row_part1}">Сведения о лице, заполнившем ДТЭГ, дата
										<br/>
										<span>											
											<xsl:value-of select="ecd:BrokerRegistryDocDetails/RUDECLcat:DocKindCode"/>
											<xsl:if test="ecd:BrokerRegistryDocDetails/RUDECLcat:RegistrationNumberId">
												<xsl:text>, </xsl:text>
												<xsl:value-of select="ecd:BrokerRegistryDocDetails/RUDECLcat:RegistrationNumberId"/>
											</xsl:if>
											<br/>
											<xsl:value-of select="ecd:SignatoryPerson/ecd:SigningDetails/cat_ru:PersonSurname"/>
											<xsl:text> </xsl:text>
											<xsl:value-of select="ecd:SignatoryPerson/ecd:SigningDetails/cat_ru:PersonName"/>
											<xsl:text> </xsl:text>
											<xsl:value-of select="ecd:SignatoryPerson/ecd:SigningDetails/cat_ru:PersonMiddleName"/>
											<br/>
											<xsl:apply-templates mode="identitycard" select="ecd:SignatoryPerson/ecd:SignatoryPersonIdentityDetails"/>
											<br/>
											<xsl:value-of select="ecd:SignatoryPerson/ecd:SigningDetails/cat_ru:PersonPost"/>
											<br/>
											<xsl:value-of select="ecd:SignatoryPerson/ecd:PowerOfAttorneyDetails/catESAD_cu:DocKindCode"/>
											<br/>
											<xsl:call-template name="russian_date"><xsl:with-param name="dateIn" select="ecd:SignatoryPerson/ecd:SigningDetails/RUScat_ru:SigningDate"/></xsl:call-template>											
										</span>
										<span>
											<br/><br/>											
											<div class="main_title">C</div>
											<br/>
											<xsl:value-of select="/ecd:ExpressCargoDeclaration/ecd:GoodsShipment[2]/ecd:HouseShipment[1]/ecd:Design[1]"/>
											<xsl:text> </xsl:text>
											<xsl:value-of select="/ecd:ExpressCargoDeclaration/ecd:GoodsShipment[2]/ecd:HouseShipment[1]/ecd:TaxBase_DecisionDate[1]"/>
											<xsl:if test="ecd:Inspector">
												<br/>
												<xsl:value-of select="ecd:Inspector"/>
												<xsl:if test="ecd:LNP">
													<xsl:text> ЛНП: </xsl:text>
													<xsl:value-of select="ecd:LNP"/>	
												</xsl:if>												
											</xsl:if>
										</span>									
										<span>
											<br/>
											<div class="main_title">D</div>
											<br/>
											<xsl:value-of select="ecd:GoodsShipment[1]/ecd:HouseShipment[1]/ecd:BadReason"/>
										</span>
									</td>
								</tr>
								<tr align="center" valign="top" style="font-size: 10pt;">
									<td class="bordered">Товар</td>
									<td class="bordered">Вид</td>
									<td class="bordered">База для исчисления</td>
									<td class="bordered">Код базы для исчисления</td>
									<td class="bordered">Ставка</td>
									<td class="bordered">Сумма</td>
								</tr>
								<tr align="center" style="font-style:italic;font-size: 10pt;">
									<td class="bordered">1</td>
									<td class="bordered">2</td>
									<td class="bordered">3</td>
									<td class="bordered">4</td>
									<td class="bordered">5</td>
									<td class="bordered">6</td>
								</tr>
								<xsl:variable name="summNalog"/>
								<xsl:for-each select="ecd:GoodsShipment/ecd:HouseShipment">									
									<xsl:variable name="party" select="."/>
									<xsl:variable name="objectordinal" select="ecd:ObjectOrdinal"/>
									<xsl:variable name="goods_num" select="ecd:GoodsItemDetails/ecd:GoodsNumeric"/>
									<!--<xsl:for-each select="ecd:GoodsItemDetails">-->
										<xsl:variable name="partyDetails" select="."/>
										<xsl:variable name="cnt_CustomsPaymentDetails" select="count(ecd:CustomsPaymentDetails)"/>
										<xsl:for-each select="ecd:CustomsPaymentDetails">
											<tr align="center" style="font-size: 9pt;">
												<xsl:if test="position()=1">
													<td class="bordered" rowspan="{$cnt_CustomsPaymentDetails}">
														<xsl:value-of select="$objectordinal"/>												
														<xsl:text>/</xsl:text>
														<xsl:value-of select="$goods_num"/>
													</td>
												</xsl:if>
												<td class="bordered">
													<xsl:value-of select="RUDECLcat:PaymentModeCode"/>
												</td>
												<td class="bordered">													
													<xsl:value-of select="RUDECLcat:TaxBase"/>
												</td>
												<td class="bordered">													
													<xsl:value-of select="RUDECLcat:TaxBaseCurrencyCode"/>
												</td>
												<td class="bordered">
													<xsl:apply-templates mode="rate1" select="."/>
												</td>												
												<td class="bordered">
													<xsl:value-of select="RUDECLcat:PaymentAmount"/>													
												</td>
												<!--<xsl:if test="position()=1">-->
												<!--	<td class="main_title" align="left">C</td>-->
												<!--</xsl:if>
														-->
											</tr>
										<!--</xsl:for-each>-->
																			
									</xsl:for-each>
									<xsl:for-each select="ecd:WayBillPaymentAmountDetails">
									<tr align="center" style="font-size: 10pt;" valign="top">
											
											<xsl:if test="position()=1">
												<td class="bordered_fat2" align="left"  style="border-bottom: none;">
												<xsl:text>Всего по индивидуальной накладной</xsl:text>
												</td>
											</xsl:if>
											
											<xsl:if test="position() &gt; 1">
												<td class="bordered_fat2" align="left" style="border-top: none;"/>
											</xsl:if>
										
											<td class="bordered_fat2">
												<xsl:value-of select="ecd:PaymentModeCode"/>
											</td>
											<td class="bordered_fat2"/>
											<td class="bordered_fat2"/>
											<td class="bordered_fat2"/>												
											<td class="bordered_fat2">
												<xsl:value-of select="ecd:Amount"/>
											</td>									
											
																					
									</tr>
									</xsl:for-each>
								</xsl:for-each>
								<tr align="center" valign="top" style="font-size: 10pt;">									
									
										<td class="bordered_fat2" align="left">
											<xsl:text>Всего по декларации на товары для экспресс-грузов</xsl:text>																					
										</td>
										<td class="bordered_fat3">
											<xsl:value-of select="ecd:GoodsShipment/ecd:FactCustomsPayment/catESAD_cu:PaymentModeCode"/>
										</td>
										<td class="bordered_fat3"/>
										<td class="bordered_fat3"/>
										<td class="bordered_fat3"/>
										<td class="bordered_fat3">
											<xsl:value-of select="ecd:GoodsShipment/ecd:FactCustomsPayment/catESAD_cu:PaymentAmount"/>
										</td>
									<!--<td align="left" style="font-size: 10pt;">C										
										<br/>
										<span>
											<xsl:value-of select="ecd:GoodsShipment[1]/ecd:HouseShipment[1]/ecd:Design"/>
										</span>
									</td>-->
									
								</tr>							
								
								<tr>
									<td class="bordered" colspan="6">
										<div width="100%" align="center" class="main_title">B1. Подробности уплаты (взыскания)</div>
										<br/>
										<xsl:for-each select="ecd:GoodsShipment/ecd:FactCustomsPayment">
											<xsl:if test="position()!=1"><br/></xsl:if>
											<xsl:value-of select="catESAD_cu:PaymentModeCode"/>
											<xsl:text>-</xsl:text>
											<xsl:value-of select="catESAD_cu:PaymentAmount"/>
											<xsl:text>-</xsl:text>
											<xsl:value-of select="catESAD_cu:PaymentCurrencyCode"/>
											<xsl:text>-</xsl:text>
											<xsl:if test="ecd:PaymentDocument">
												<xsl:text>-</xsl:text>
												<xsl:call-template name="russian_date"><xsl:with-param name="dateIn" select="ecd:PaymentDocument/cat_ru:PrDocumentDate"/></xsl:call-template>
											</xsl:if>
											<xsl:if test="count(ecd:PaymentDocument)=0">
												<xsl:text>-</xsl:text>
												<xsl:text>00</xsl:text>
											</xsl:if>
										<!--	<xsl:choose>
												<xsl:when test="ecd:PaymentDocument/cat_ru:PrDocumentDate">
													<xsl:call-template name="russian_date"><xsl:with-param name="dateIn" select="ecd:PaymentDocument/cat_ru:PrDocumentDate"/></xsl:call-template>
												</xsl:when>
												<xsl:otherwise>00</xsl:otherwise>
											</xsl:choose>-->
											<!--<xsl:text>-</xsl:text>-->
											<!--<xsl:value-of select="ecd:PaymentWayCode"/>-->
											<xsl:text>-</xsl:text>
											<xsl:if test="ecd:RFOrganizationFeatures/cat_ru:INN">												
												<xsl:value-of select="ecd:RFOrganizationFeatures/cat_ru:INN"/>
											</xsl:if>
										</xsl:for-each>
									</td>
									<!--<td align="left" style="font-size: 10pt;">D
										<br/>
										<span>
											<xsl:value-of select="ecd:GoodsShipment[1]/ecd:HouseShipment[1]/ecd:BadReason"/>
										</span>
									</td>-->
								</tr>							
								
							</tbody>
						</table>						
					</xsl:if>
						<xsl:if test="ecd:ExpressRegistryKindCode = 'ПТДЭГ'">
						<table cellpadding="4">
							<tbody>
								<tr>
									<td/>
									<td/>
									<td/>
									<td/>
									<td/>
									<td/>
									<td/>
									<td/>
									<td/>
									<td/>
									<td/>
									<td/>
									<td/>
									<td/>
									<td/>
									<td/>
									<td/>
								</tr>
								<tr>
									<td colspan="17" style="border-top:1px solid black;border-left:1px solid black; border-right:1px solid black;" align="center" valign="middle"><b>Пассажирская таможенная декларация для экспресс-грузов</b></td>
								</tr>
								<tr valign="top">
									<td class="bordered main_title" colspan="5" rowspan="3">Отправитель (по общей накладной)<br/>
										<span>
											<xsl:apply-templates mode="org" select="ecd:GoodsShipment/ecd:ConsignorDetails"/>
										</span>										
									</td>
									<td class="bordered main_title" colspan="5" rowspan="3">Получатель (по общей накладной)<br/>
										<span>
											<xsl:apply-templates mode="org" select="ecd:GoodsShipment/ecd:ConsigneeDetails"/>
										</span>										
									</td>
									<td class="bordered main_title" colspan="4" rowspan="2">A
									<br/>
										<span style="font-size:14pt;">
											<xsl:value-of select="ecd:RegNUM"/>
										</span>	
									</td>
									<td class="bordered main_title" colspan="2">ПТДЭГ</td>
									<td class="bordered main_title" rowspan="2">Особенность<br/>
										<span>
											<xsl:value-of select="ecd:DeclarationKind"/>
										</span>										
									</td>
								</tr>
								<tr>
									<td style="border-right: 1px solid black;">
										<span>
											<xsl:value-of select="ecd:DeclarationKindCode"/>
										</span>										
									</td>
									<td>
										<span>
											<xsl:value-of select="ecd:ElectronicDocumentSign"/>	
										</span>										
									</td>
								</tr>
								<tr valign="top">
									<td class="bordered main_title" colspan="4">Предшествующий&#160;документ<br/>										
										<span>
											<xsl:call-template name="PrecedingDocDetails_info"/>
										</span>										
									</td>
									<td class="bordered main_title" colspan="3">Кол-во листов 
										<span><xsl:value-of select="ecd:TotalSheetNumber"/></span>										
									</td>
								</tr>
								<tr align="center" valign="top" style="font-size: 10pt;font-weight:bold;">
									<td class="bordered" colspan="5">Общие сведения</td>
									<td class="bordered" colspan="7">Сведения о товарах</td>
									<td class="bordered" colspan="2">Сведения о документах</td>
									<td class="bordered" colspan="3" rowspan="3">Примечание</td>
								</tr>
								<tr align="center" valign="top" style="font-size: 10pt;">
									<td class="bordered">N п/п</td>
									<td class="bordered">Общая накладная</td>
									<td class="bordered">Инд. накладная</td>
									<td class="bordered">Инд. отправитель<sup>1</sup></td>
									<td class="bordered">Инд. получатель<sup>2</sup></td>
									<td class="bordered">N п/п</td>
									<td class="bordered">Наименование</td>
									<td class="bordered">Код ТН ВЭД ЕАЭС</td>
									<td class="bordered">Кол-во</td>
									<td class="bordered">Вес брутто</td>
									<td class="bordered">Валюта, стоимость</td>
									<td class="bordered">Валюта, стоимость в валюте государства-члена</td>
									<td class="bordered">Код, признак</td>
									<td class="bordered">Дата, номер</td>
								</tr>
								<tr align="center" style="font-style:italic;font-size: 10pt;">
									<td class="bordered">1</td>
									<td class="bordered">2</td>
									<td class="bordered">3</td>
									<td class="bordered">4</td>
									<td class="bordered">5</td>
									<td class="bordered">6</td>
									<td class="bordered">7</td>
									<td class="bordered">8</td>
									<td class="bordered">9</td>
									<td class="bordered">10</td>
									<td class="bordered">11</td>
									<td class="bordered">12</td>
									<td class="bordered">13</td>
									<td class="bordered">14</td>
								</tr>
								<xsl:for-each select="ecd:GoodsShipment/ecd:HouseShipment">
									<xsl:variable name="tov" select="count(ecd:GoodsItemDetails)"/>
									<xsl:variable name="rowspan_p" select="count(ecd:GoodsItemDetails[not(ecd:PresentedDocDetails)])+count(ecd:GoodsItemDetails/ecd:PresentedDocDetails)"/>
									<tr align="center" valign="top" style="font-size: 9pt;">
										<td class="bordered">
											<xsl:if test="$rowspan_p &gt; 1">												
												<xsl:attribute name="rowspan"><xsl:value-of select="$rowspan_p"/></xsl:attribute>
											</xsl:if>
											<xsl:value-of select="ecd:ObjectOrdinal"/>
										</td>
										<td class="bordered">
											<xsl:if test="$rowspan_p &gt; 1">
												<xsl:attribute name="rowspan"><xsl:value-of select="$rowspan_p"/></xsl:attribute>
											</xsl:if>
											<xsl:apply-templates mode="doc" select="ecd:TransportDocumentDetails"/>
										</td>
										<td class="bordered">
											<xsl:if test="$rowspan_p &gt; 1">
												<xsl:attribute name="rowspan"><xsl:value-of select="$rowspan_p"/></xsl:attribute>
											</xsl:if>
											<xsl:apply-templates mode="doc" select="ecd:HouseWaybillDetails"/>
										</td>
										<td class="bordered">
											<xsl:if test="$rowspan_p &gt; 1">
												<xsl:attribute name="rowspan"><xsl:value-of select="$rowspan_p"/></xsl:attribute>
											</xsl:if>
											<xsl:apply-templates mode="org" select="ecd:ConsignorDetails"/>
										</td>
										<td class="bordered">
											<xsl:if test="$rowspan_p &gt; 1">
												<xsl:attribute name="rowspan"><xsl:value-of select="$rowspan_p"/></xsl:attribute>
											</xsl:if>
											<xsl:apply-templates mode="org" select="ecd:ConsigneeDetails"/>
										</td>
										<xsl:if test="not(ecd:GoodsItemDetails)">											
											<td class="bordered"/>
											<td class="bordered"/>
											<td class="bordered"/>
											<td class="bordered"/>
											<td class="bordered"/>
											<td class="bordered"/>
											<td class="bordered"/>
											<td class="bordered"/>
											<td class="bordered"/>
											<td class="bordered"/>
										</xsl:if>
										<xsl:apply-templates select="ecd:GoodsItemDetails[1]" mode="ptd"/>
									</tr>
									<xsl:for-each select="ecd:GoodsItemDetails[position() = 1]">
										<xsl:for-each select="ecd:PresentedDocDetails[position() &gt; 1]">
											<tr align="center" style="font-size: 9pt;" valign="top">
												<xsl:apply-templates select="."/>
											</tr>
										</xsl:for-each>
									</xsl:for-each>
									<xsl:for-each select="ecd:GoodsItemDetails[position() &gt; 1]">
										<tr align="center" style="font-size: 9pt;" valign="top">
											<xsl:apply-templates select="." mode="ptd"/>
										</tr>
										<xsl:for-each select="ecd:PresentedDocDetails[position() &gt; 1]">
											<tr align="center" style="font-size: 9pt;" valign="top">
												<xsl:apply-templates select="."/>
											</tr>
										</xsl:for-each>
									</xsl:for-each>
									<tr align="center" valign="top" style="font-size: 9pt;">										
										<td class="bordered_fat2" colspan="9" align="left">
												Всего по индивидуальной накладной<br/>
											<span style="font-style:italic;font-size: 8pt;">(общий вес брутто, стоимость в валюте государства-члена)</span>
										</td>
										<xsl:if test="$tov = 1">
											<!-- если один товар -->
											<!--<td  class="bordered_fat2" />-->
										</xsl:if>
										<td class="bordered_fat2">
											<xsl:apply-templates mode="quantity" select="ecd:UnifiedGrossWeightQuantity"/>
										</td>
										<td  class="bordered_fat2"/>
										<td class="bordered_fat2">
											<xsl:if test="ecd:GoodsItemDetails[position() = 1]">
												<xsl:apply-templates mode="pricevalue" select="ecd:CustomsCost"/>
											</xsl:if>
										</td>
										<td  class="bordered_fat2" colspan="2"/>
										<td class="bordered_fat2"  colspan="3" style="font-style:italic;font-size: 8pt;">
											<xsl:value-of select="ecd:Design"/>												
											<xsl:text> </xsl:text>
											<xsl:value-of select="ecd:TaxBase_DecisionDate"/>
										</td>
									</tr>
								</xsl:for-each>
								<tr align="center" valign="top" style="font-size: 9pt;">
									<td class="bordered_fat3" style="font-weight:bold;" colspan="9" align="left">
											Всего по пассажирской таможенной декларации для экспресс-грузов<br/>
										<span style="font-style:italic;font-size: 8pt;">
											(общий вес брутто, стоимость в валюте государства-члена)
										</span>
										</td>
									<td class="bordered_fat3" style="font-weight:bold;">
										<!-- <xsl:apply-templates mode="quantity" select="ecd:GoodsShipment/ecd:UnifiedGrossWeightQuantity"/> -->
										<xsl:value-of select="format-number(sum(//ecd:GoodsShipment/ecd:HouseShipment/ecd:UnifiedGrossWeightQuantity/cat_ru:GoodsQuantity), '###0.000')"/>
									</td>
									<td class="bordered_fat3">
										
									</td>
									<td class="bordered_fat3" style="font-weight:bold;">										
										<!--<xsl:apply-templates mode="pricevalue" select="ecd:GoodsShipment/ecd:CAValueAmount"/>-->
										<!-- <xsl:apply-templates mode="pricevalue" select="ecd:GoodsShipment/ecd:CustomsCost"/> -->
										<xsl:value-of select="format-number(sum(//ecd:GoodsShipment/ecd:HouseShipment/ecd:CustomsCost/ecd:CurrencyQuantity), '###0.00')"/>
									</td>
									<td class="bordered_fat3" colspan="5"/>
								</tr>
								<tr>
									<td colspan="17"><br/> </td>
								</tr>
								<tr>
									<td class="bordered main_title" colspan="17">Сведения о лице, заполнившем ПТДЭГ, дата
										<br/>
										<span>
											<xsl:value-of select="ecd:BrokerRegistryDocDetails/RUDECLcat:DocKindCode"/>
											<xsl:if test="ecd:BrokerRegistryDocDetails/RUDECLcat:RegistrationNumberId">
												<xsl:text>, </xsl:text>
												<xsl:value-of select="ecd:BrokerRegistryDocDetails/RUDECLcat:RegistrationNumberId"/>
											</xsl:if>
											<br/>
											<xsl:value-of select="ecd:SignatoryPerson/ecd:SigningDetails/cat_ru:PersonSurname"/>
											<xsl:text> </xsl:text>
											<xsl:value-of select="ecd:SignatoryPerson/ecd:SigningDetails/cat_ru:PersonName"/>
											<xsl:text> </xsl:text>
											<xsl:value-of select="ecd:SignatoryPerson/ecd:SigningDetails/cat_ru:PersonMiddleName"/>
											<br/>
											<xsl:apply-templates mode="identitycard" select="ecd:SignatoryPerson/ecd:SignatoryPersonIdentityDetails"/>
											<br/>
											<xsl:value-of select="ecd:SignatoryPerson/ecd:SigningDetails/cat_ru:PersonPost"/>
											<br/>
											<xsl:value-of select="ecd:SignatoryPerson/ecd:PowerOfAttorneyDetails/catESAD_cu:DocKindCode"/>
											<br/>
											<xsl:call-template name="russian_date">
												<xsl:with-param name="dateIn" select="ecd:SignatoryPerson/ecd:SigningDetails/RUScat_ru:SigningDate"/>
											</xsl:call-template>
										</span>										
									</td>
								</tr>
								<tr>
									<td class="bordered main_title" colspan="17" valign="top">C<br/>
										<span>
											<xsl:value-of select="/ecd:ExpressCargoDeclaration/ecd:GoodsShipment[2]/ecd:HouseShipment[1]/ecd:Design[1]"/>
											<xsl:text> </xsl:text>
											<xsl:value-of select="/ecd:ExpressCargoDeclaration/ecd:GoodsShipment[2]/ecd:HouseShipment[1]/ecd:TaxBase_DecisionDate[1]"/>
											<xsl:if test="ecd:Inspector">
												<br/>
												<xsl:value-of select="ecd:Inspector"/>
												<xsl:if test="ecd:LNP">
													<xsl:text> ЛНП: </xsl:text>
													<xsl:value-of select="ecd:LNP"/>	
												</xsl:if>												
											</xsl:if>
										</span>										
									</td>
								</tr>
								<tr>
									<td class="bordered main_title" colspan="17" valign="top">D<br/>
										<span><xsl:value-of select="ecd:GoodsShipment[1]/ecd:HouseShipment[1]/ecd:BadReason"/></span>
									</td>
								</tr>
							</tbody>
						</table>
					</xsl:if>
					</xsl:if>
					<xsl:if test="ecd:DocType = '1'">
						<xsl:if test="ecd:ExpressRegistryKindCode = 'ДТЭГ'">
						<table cellpadding="4" >
							<tbody>
								<tr>
									<td/>
									<td/>
									<td/>
									<td/>
									<td/>
									<td/>
									<td/>
									<td/>
									<td/>
									<td/>
									<td/>
									<td/>
									<td/>
									<td/>
									<td/>
									<td/>
								</tr>
								<tr>
									<td colspan="16" style="border-top:1px solid black;border-left:1px solid black; border-right:1px solid black;" align="center" valign="middle"><b>Корректировка декларации на товары для экспресс-грузов</b></td>
								</tr>
								<tr valign="top">
									<td class="bordered main_title" colspan="4" rowspan="2">
										Отправитель (по общей накладной)<br/>
										<span><xsl:apply-templates mode="org" select="ecd:GoodsShipment/ecd:ConsignorDetails"/></span>
									</td>
									<td class="bordered main_title" colspan="3" rowspan="2">Получатель (по общей накладной)<br/>
										<span><xsl:apply-templates mode="org" select="ecd:GoodsShipment/ecd:ConsigneeDetails"/></span>	
									</td>
									<td colspan="5" class="bordered main_title">A<br/>
										<xsl:if test="$ExpressCargoDeclarationCustomMark">
											<xsl:apply-templates mode="reg_num" select="$ExpressCargoDeclarationCustomMark/ecdcm:ApplicationRegNumber"/>
										</xsl:if>
										<span style="font-size:14pt;">
											<xsl:value-of select="ecd:RegNUM"/>
										</span>											
									</td>
									<td colspan="2" class="bordered main_title">КДТЭГ<br/>
										<span>
											<xsl:value-of select="ecd:ElectronicDocumentSign"/>
										</span>										
									</td>
									<td colspan="2" class="bordered main_title">Особенность<br/>
										<span>
											<xsl:value-of select="ecd:DeclarationKind"></xsl:value-of>
										</span>
									</td>
								</tr>
								<tr valign="top">
									<td colspan="5" class="bordered main_title">Предшествующий документ<br/>
										<span>
											<xsl:call-template name="PrecedingDocDetails_info"/>
										</span>
									</td>
									<td colspan="2" class="bordered main_title">Кол-во листов 
										<span><xsl:value-of select="ecd:TotalSheetNumber"/></span>										
									</td>
									<td colspan="2" class="bordered main_title">Процедура<br/>
										<span>
											<xsl:value-of select="ecd:CustomsModeCode"/>
											<xsl:text> </xsl:text>
											<xsl:value-of select="ecd:PreviousCustomsModeCode"/>
										</span>										
									</td>
								</tr>
								<tr align="center" valign="top" style="font-size: 10pt;font-weight:bold;">
									<td class="bordered" colspan="5">Общие сведения</td>
									<td class="bordered" colspan="8">Сведения о товарах</td>
									<td class="bordered" colspan="2">Сведения о документах</td>
									<td class="bordered" >Код изменений</td>
								</tr>
								<tr align="center" valign="top" style="font-size: 10pt;">
									<td class="bordered" rowspan="2">N п/п</td>
									<td class="bordered" rowspan="2">Общая накладная</td>
									<td class="bordered" rowspan="2">Инд. накладная</td>
									<td class="bordered" rowspan="2">Инд. отправитель<sup>1</sup></td>
									<td class="bordered" rowspan="2">Инд. получатель<sup>2</sup></td>
									<td class="bordered" rowspan="2">N п/п</td>
									<td class="bordered" rowspan="2">Наименование</td>
									<td class="bordered" rowspan="2">Код ТН ВЭД ЕАЭС</td>
									<td class="bordered" rowspan="2">Кол-во</td>
									<td class="bordered" colspan="2">Вес</td>
									<td class="bordered" rowspan="2">Валюта, стоимость</td>
									<td class="bordered" rowspan="2">Таможенная стоимость</td>
									<td class="bordered" rowspan="2">Код, признак</td>
									<td class="bordered" style="font-size: 10pt;" rowspan="2">Дата, номер</td>
									<td class="bordered" rowspan="3"/>
								</tr>
								<tr align="center" valign="top" style="font-size: 10pt;">
									<td class="bordered">брутто</td>
									<td class="bordered">нетто</td>
								</tr>
								<tr align="center" style="font-style:italic;font-size: 10pt;">
									<td class="bordered">1</td>
									<td class="bordered">2</td>
									<td class="bordered">3</td>
									<td class="bordered">4</td>
									<td class="bordered">5</td>
									<td class="bordered">6</td>
									<td class="bordered">7</td>
									<td class="bordered">8</td>
									<td class="bordered">9</td>
									<td class="bordered">10</td>
									<td class="bordered">11</td>
									<td class="bordered">12</td>
									<td class="bordered">13</td>
									<td class="bordered">14</td>
									<td class="bordered">15</td>
								</tr>																
								<xsl:for-each select="ecd:GoodsShipment/ecd:HouseShipment">
									<xsl:variable name="tov" select="count(ecd:GoodsItemDetails)"/>
									<xsl:variable name="rowspan" select="count(ecd:GoodsItemDetails[not(ecd:PresentedDocDetails)])+count(ecd:GoodsItemDetails/ecd:PresentedDocDetails)"/>
									<tr align="center" valign="top" style="font-size: 9pt;">
										<td class="bordered">
											<xsl:if test="$rowspan &gt; 0">
												<xsl:attribute name="rowspan"><xsl:value-of select="$rowspan+1"/></xsl:attribute>
											</xsl:if>
											<xsl:value-of select="ecd:ObjectOrdinal"/>
										</td>
										<td class="bordered">
											<xsl:if test="$rowspan &gt; 1">
												<xsl:attribute name="rowspan"><xsl:value-of select="$rowspan"/></xsl:attribute>
											</xsl:if>
											<xsl:apply-templates mode="doc" select="ecd:TransportDocumentDetails"/>
										</td>
										<td class="bordered">
											<xsl:if test="$rowspan &gt; 1">
												<xsl:attribute name="rowspan"><xsl:value-of select="$rowspan"/></xsl:attribute>
											</xsl:if>
											<xsl:apply-templates mode="doc" select="ecd:HouseWaybillDetails"/>
										</td>
										<td class="bordered">
											<xsl:if test="$rowspan &gt; 1">
												<xsl:attribute name="rowspan"><xsl:value-of select="$rowspan"/></xsl:attribute>
											</xsl:if>
											<xsl:apply-templates mode="org" select="ecd:ConsignorDetails"/>
										</td>
										<td class="bordered">
											<xsl:if test="$rowspan &gt; 1">
												<xsl:attribute name="rowspan"><xsl:value-of select="$rowspan"/></xsl:attribute>
											</xsl:if>
											<xsl:apply-templates mode="org" select="ecd:ConsigneeDetails"/>
										</td>
										<xsl:if test="not(ecd:GoodsItemDetails)">
											<td class="bordered"/>
											<td class="bordered"/>
											<td class="bordered"/>
											<td class="bordered"/>
											<td class="bordered"/>
											<td class="bordered"/>
											<td class="bordered"/>
											<td class="bordered"/>
											<td class="bordered"/>
											<td class="bordered"/>
											<td class="bordered"/>
										</xsl:if>
										<xsl:apply-templates select="ecd:GoodsItemDetails[1]" mode="dt"/>
									</tr>
									<xsl:for-each select="ecd:GoodsItemDetails[position() = 1]">
										<xsl:for-each select="ecd:PresentedDocDetails[position() &gt; 1]">
											<tr align="center" valign="top" style="font-size: 9pt;">												
												<xsl:apply-templates select="."/>
											</tr>
										</xsl:for-each>
									</xsl:for-each>
									<xsl:for-each select="ecd:GoodsItemDetails[position() &gt; 1]">
										<tr align="center" valign="top" style="font-size: 9pt;">
											<xsl:apply-templates select="." mode="dt"/>
										</tr>
										<xsl:for-each select="ecd:PresentedDocDetails[position() &gt; 1]">
											<tr align="center" valign="top" style="font-size: 9pt;">
												<xsl:apply-templates select="."/>
											</tr>
										</xsl:for-each>
									</xsl:for-each>
									
									<tr align="center" valign="top" style="font-size: 10pt;">				
										<td class="bordered_fat2" colspan="8" align="left">
											Всего по индивидуальной накладной<br/>
											<span style="font-style:italic;font-size: 8pt;">
												(общий вес брутто, таможенная стоимость)
											</span>
										</td>							
										<td class="bordered_fat2" >
											<xsl:apply-templates mode="quantity" select="ecd:UnifiedGrossWeightQuantity"/>
										</td>
										<td  class="bordered_fat2" />										
										<td  class="bordered_fat2" />										
										<td class="bordered_fat2">
											<xsl:apply-templates mode="pricevalue" select="ecd:CustomsCost"/>
										</td>
										<td  class="bordered_fat2" colspan="3" style="font-style:italic;font-size: 8pt;">
											<xsl:value-of select="ecd:Design"/>
											<xsl:text> </xsl:text>
											<xsl:value-of select="ecd:TaxBase_DecisionDate"/>
										</td>										
									</tr>
									
								</xsl:for-each>
								<tr align="center" valign="top" style="font-size: 10pt;">
									<td class="bordered_fat3" style="font-weight:bold;" colspan="9" align="left">
											Всего по декларации на товары для экспресс-грузов<br/>
										<span style="font-style:italic;font-size: 8pt;">
											(общий вес брутто, таможенная стоимость)
										</span>
										</td>
									<td class="bordered_fat3" style="font-weight:bold;">
										<xsl:apply-templates mode="quantity" select="ecd:GoodsShipment/ecd:UnifiedGrossWeightQuantity"/>
									</td>
									<td class="bordered_fat3" colspan="2"/>
									<td class="bordered_fat3" style="font-weight:bold;">
										<xsl:apply-templates mode="pricevalue" select="ecd:GoodsShipment/ecd:CustomsCost"/>
									</td>
									<td class="bordered_fat3" colspan="3"/>
								</tr>
							</tbody>
						</table>
						<br/>
						<br/>
						<table cellpadding="4">
							<tbody>
								<xsl:variable name="cnt_GoodsItemDetails" select="count(ecd:GoodsShipment/ecd:HouseShipment/ecd:GoodsItemDetails/ecd:CustomsPaymentDetails)+count(ecd:GoodsShipment/ecd:HouseShipment)+4"/>
								<xsl:variable name="row_part1" select="number($cnt_GoodsItemDetails)-number(2)"/>
								<tr align="center" valign="top">
									<td class="bordered main_title" colspan="6">B. Исчисление платежей</td>
									<td class="bordered main_title" align="left" rowspan="{$row_part1}">Сведения о лице, заполнившем КДТЭГ, дата
										<br/>
										<span>											
											<xsl:value-of select="ecd:BrokerRegistryDocDetails/RUDECLcat:DocKindCode"/>
											<xsl:if test="ecd:BrokerRegistryDocDetails/RUDECLcat:RegistrationNumberId">
												<xsl:text>, </xsl:text>
												<xsl:value-of select="ecd:BrokerRegistryDocDetails/RUDECLcat:RegistrationNumberId"/>
											</xsl:if>
											<br/>
											<xsl:value-of select="ecd:SignatoryPerson/ecd:SigningDetails/cat_ru:PersonSurname"/>
											<xsl:text> </xsl:text>
											<xsl:value-of select="ecd:SignatoryPerson/ecd:SigningDetails/cat_ru:PersonName"/>
											<xsl:text> </xsl:text>
											<xsl:value-of select="ecd:SignatoryPerson/ecd:SigningDetails/cat_ru:PersonMiddleName"/>
											<br/>
											<xsl:apply-templates mode="identitycard" select="ecd:SignatoryPerson/ecd:SignatoryPersonIdentityDetails"/>
											<br/>
											<xsl:value-of select="ecd:SignatoryPerson/ecd:SigningDetails/cat_ru:PersonPost"/>
											<br/>
											<xsl:value-of select="ecd:SignatoryPerson/ecd:PowerOfAttorneyDetails/catESAD_cu:DocKindCode"/>
											<br/>
											<xsl:call-template name="russian_date"><xsl:with-param name="dateIn" select="ecd:SignatoryPerson/ecd:SigningDetails/RUScat_ru:SigningDate"/></xsl:call-template>											
										</span>
										<span>
											<br/><br/>											
											<div class="main_title">C</div>
											<br/>
											<xsl:value-of select="/ecd:ExpressCargoDeclaration/ecd:GoodsShipment[2]/ecd:HouseShipment[1]/ecd:Design[1]"/>
											<xsl:text> </xsl:text>
											<xsl:value-of select="/ecd:ExpressCargoDeclaration/ecd:GoodsShipment[2]/ecd:HouseShipment[1]/ecd:TaxBase_DecisionDate[1]"/>
											<xsl:if test="ecd:Inspector">
												<br/>
												<xsl:value-of select="ecd:Inspector"/>
												<xsl:if test="ecd:LNP">
													<xsl:text> ЛНП: </xsl:text>
													<xsl:value-of select="ecd:LNP"/>	
												</xsl:if>												
											</xsl:if>
										</span>									
										<span>
											<br/>
											<div class="main_title">D</div>
											<br/>
											<xsl:value-of select="ecd:GoodsShipment[1]/ecd:HouseShipment[1]/ecd:BadReason"/>
										</span>
									</td>
								</tr>
								<tr align="center" valign="top" style="font-size: 10pt;">
									<td class="bordered">Товар</td>
									<td class="bordered">Вид</td>
									<td class="bordered">База для исчисления</td>
									<td class="bordered">Код базы для исчисления</td>
									<td class="bordered">Ставка</td>
									<td class="bordered">Сумма</td>
								</tr>
								<tr align="center" style="font-style:italic;font-size: 10pt;">
									<td class="bordered">1</td>
									<td class="bordered">2</td>
									<td class="bordered">3</td>
									<td class="bordered">4</td>
									<td class="bordered">5</td>
									<td class="bordered">6</td>
								</tr>
								<xsl:variable name="summNalog"/>
								<xsl:for-each select="ecd:GoodsShipment/ecd:HouseShipment">									
									<xsl:variable name="party" select="."/>
									<xsl:variable name="objectordinal" select="ecd:ObjectOrdinal"/>
									<xsl:variable name="goods_num" select="ecd:GoodsItemDetails/ecd:GoodsNumeric"/>
									<!--<xsl:for-each select="ecd:GoodsItemDetails">-->
										<xsl:variable name="partyDetails" select="."/>
										<xsl:variable name="cnt_CustomsPaymentDetails" select="count(ecd:CustomsPaymentDetails)"/>
										<xsl:for-each select="ecd:CustomsPaymentDetails">
											<tr align="center" style="font-size: 9pt;">
												<xsl:if test="position()=1">
													<td class="bordered" rowspan="{$cnt_CustomsPaymentDetails}">
														<xsl:value-of select="$objectordinal"/>												
														<xsl:text>/</xsl:text>
														<xsl:value-of select="$goods_num"/>
													</td>
												</xsl:if>
												<td class="bordered">
													<xsl:value-of select="RUDECLcat:PaymentModeCode"/>
												</td>
												<td class="bordered">													
													<xsl:value-of select="RUDECLcat:TaxBase"/>
												</td>
												<td class="bordered">													
													<xsl:value-of select="RUDECLcat:TaxBaseCurrencyCode"/>
												</td>
												<td class="bordered">
													<xsl:apply-templates mode="rate1" select="."/>
												</td>												
												<td class="bordered">
													<xsl:value-of select="RUDECLcat:PaymentAmount"/>													
												</td>											
											</tr>
										<!--</xsl:for-each>-->
																			
									</xsl:for-each>
									<xsl:for-each select="ecd:WayBillPaymentAmountDetails">
									<tr align="center" style="font-size: 10pt;" valign="top">
											
											<xsl:if test="position()=1">
												<td class="bordered_fat2" align="left"  style="border-bottom: none;">
												<xsl:text>Всего по индивидуальной накладной</xsl:text>
												</td>
											</xsl:if>
											
											<xsl:if test="position() &gt; 1">
												<td class="bordered_fat2" align="left" style="border-top: none;"/>
											</xsl:if>
										
											<td class="bordered_fat2">
												<xsl:value-of select="ecd:PaymentModeCode"/>
											</td>
											<td class="bordered_fat2"/>
											<td class="bordered_fat2"/>
											<td class="bordered_fat2"/>												
											<td class="bordered_fat2">
												<xsl:value-of select="ecd:Amount"/>
											</td>									
											
																					
									</tr>
									</xsl:for-each>
								</xsl:for-each>
								<tr align="center" valign="top" style="font-size: 10pt;">									
									
										<td class="bordered_fat2" align="left">
											<xsl:text>Всего по декларации на товары для экспресс-грузов</xsl:text>																					
										</td>
										<td class="bordered_fat3">
											<xsl:value-of select="ecd:GoodsShipment/ecd:FactCustomsPayment/catESAD_cu:PaymentModeCode"/>
										</td>
										<td class="bordered_fat3"/>
										<td class="bordered_fat3"/>
										<td class="bordered_fat3"/>
										<td class="bordered_fat3">
											<xsl:value-of select="ecd:GoodsShipment/ecd:FactCustomsPayment/catESAD_cu:PaymentAmount"/>
										</td>								
								</tr>							
								
								<tr>
									<td class="bordered" colspan="6">
										<div width="100%" align="center" class="main_title">B1. Подробности уплаты (взыскания)</div>
										<br/>
										<xsl:for-each select="ecd:GoodsShipment/ecd:FactCustomsPayment">
											<xsl:if test="position()!=1"><br/></xsl:if>
											<xsl:value-of select="catESAD_cu:PaymentModeCode"/>
											<xsl:text>-</xsl:text>
											<xsl:value-of select="catESAD_cu:PaymentAmount"/>
											<xsl:text>-</xsl:text>
											<xsl:value-of select="catESAD_cu:PaymentCurrencyCode"/>
											<xsl:text>-</xsl:text>
											<xsl:if test="ecd:PaymentDocument">
												<xsl:text>-</xsl:text>
												<xsl:call-template name="russian_date"><xsl:with-param name="dateIn" select="ecd:PaymentDocument/cat_ru:PrDocumentDate"/></xsl:call-template>
											</xsl:if>
											<xsl:if test="count(ecd:PaymentDocument)=0">
												<xsl:text>-</xsl:text>
												<xsl:text>00</xsl:text>
											</xsl:if>										
											<xsl:text>-</xsl:text>
											<xsl:if test="ecd:RFOrganizationFeatures/cat_ru:INN">												
												<xsl:value-of select="ecd:RFOrganizationFeatures/cat_ru:INN"/>
											</xsl:if>
										</xsl:for-each>
									</td>
								</tr>
							</tbody>
						</table>						
					</xsl:if>
						<xsl:if test="ecd:ExpressRegistryKindCode = 'ПТДЭГ'">
							<table cellpadding="4">
								<tbody>
									<tr>
										<td/>
										<td/>
										<td/>
										<td/>
										<td/>
										<td/>
										<td/>
										<td/>
										<td/>
										<td/>
										<td/>
										<td/>
										<td/>
										<td/>
										<td/>
										<td/>
										<td/>
									</tr>
									<tr>
										<td colspan="17" style="border-top:1px solid black;border-left:1px solid black; border-right:1px solid black;" align="center" valign="middle">
											<b>Корректировка пассажирской таможенной декларации для экспресс-грузов</b>
										</td>
									</tr>
									<tr valign="top">
										<td class="bordered" colspan="5" rowspan="3">Отправитель (по общей накладной)<br/>
											<xsl:apply-templates mode="org" select="ecd:GoodsShipment/ecd:ConsignorDetails"/>
										</td>
										<td class="bordered" colspan="5" rowspan="3">Получатель (по общей накладной)<br/>
											<xsl:apply-templates mode="org" select="ecd:GoodsShipment/ecd:ConsigneeDetails"/>
										</td>
										<td class="bordered" colspan="4" rowspan="2">
											<xsl:text>A</xsl:text>
											<br/>
											<xsl:if test="$ExpressCargoDeclarationCustomMark">
												<xsl:apply-templates mode="reg_num" select="$ExpressCargoDeclarationCustomMark/ecdcm:ApplicationRegNumber"/>
											</xsl:if>
										</td>
										<td class="bordered" colspan="2">КПТДЭГ</td>
										<td class="bordered" rowspan="2">Особенность<br/>
											<xsl:apply-templates select="ecd:DeclarationKind"/>
										</td>
									</tr>
									<tr>
										<td style="border-right: 1px solid black;">
											<xsl:apply-templates select="ecd:DeclarationKindCode"/>
										</td>
										<td>
											<xsl:apply-templates select="ecd:ElectronicDocumentSign"/>
										</td>
									</tr>
									<tr valign="top">
										<td class="bordered" colspan="4">Предшествующий&#160;документ<br/>
											<span>
												<xsl:apply-templates select="//ecd:PrecedingDocDetails"/>
											</span>											
										</td>
										<td class="bordered" colspan="3">Кол-во листов <xsl:value-of select="ecd:TotalSheetNumber"/>
										</td>
									</tr>
									<tr align="center" valign="top">
										<td class="bordered" colspan="5">Общие сведения</td>
										<td class="bordered" colspan="7">Сведения о товарах</td>
										<td class="bordered" colspan="2">Сведения о документах</td>
										<td class="bordered" colspan="3" rowspan="3">Код изменений</td>
									</tr>
									<tr align="center" valign="top">
										<td class="bordered">N п/п</td>
										<td class="bordered">Общая накладная</td>
										<td class="bordered">Инд. накладная</td>
										<td class="bordered">Инд. отправитель<sup>1</sup>
										</td>
										<td class="bordered">Инд. получатель<sup>2</sup>
										</td>
										<td class="bordered">N п/п</td>
										<td class="bordered">Наименование</td>
										<td class="bordered">Код ТН ВЭД ЕАЭС</td>
										<td class="bordered">Кол-во</td>
										<td class="bordered">Вес брутто</td>
										<td class="bordered">Валюта, стоимость</td>
										<td class="bordered">Валюта, стоимость в валюте государства-члена</td>
										<td class="bordered">Код, признак</td>
										<td class="bordered">Дата, номер</td>
									</tr>
									<tr align="center">
										<td class="bordered">1</td>
										<td class="bordered">2</td>
										<td class="bordered">3</td>
										<td class="bordered">4</td>
										<td class="bordered">5</td>
										<td class="bordered">6</td>
										<td class="bordered">7</td>
										<td class="bordered">8</td>
										<td class="bordered">9</td>
										<td class="bordered">10</td>
										<td class="bordered">11</td>
										<td class="bordered">12</td>
										<td class="bordered">13</td>
										<td class="bordered">14</td>
									</tr>
									<xsl:for-each select="ecd:GoodsShipment/ecd:HouseShipment">
										<xsl:variable name="rowspan_p" select="count(ecd:GoodsItemDetails[not(ecd:PresentedDocDetails)])+count(ecd:GoodsItemDetails/ecd:PresentedDocDetails)"/>
										<tr align="center" valign="top">
											<td class="bordered">
												<xsl:if test="$rowspan_p &gt; 1">
													<xsl:attribute name="rowspan"><xsl:value-of select="$rowspan_p+1"/></xsl:attribute>
												</xsl:if>
												<xsl:apply-templates select="ecd:ObjectOrdinal"/>
											</td>
											<td class="bordered">
												<xsl:if test="$rowspan_p &gt; 1">
													<xsl:attribute name="rowspan"><xsl:value-of select="$rowspan_p"/></xsl:attribute>
												</xsl:if>
												<xsl:apply-templates mode="doc" select="ecd:TransportDocumentDetails"/>
											</td>
											<td class="bordered">
												<xsl:if test="$rowspan_p &gt; 1">
													<xsl:attribute name="rowspan"><xsl:value-of select="$rowspan_p"/></xsl:attribute>
												</xsl:if>
												<xsl:apply-templates mode="doc" select="ecd:HouseWaybillDetails"/>
											</td>
											<td class="bordered">
												<xsl:if test="$rowspan_p &gt; 1">
													<xsl:attribute name="rowspan"><xsl:value-of select="$rowspan_p"/></xsl:attribute>
												</xsl:if>
												<xsl:apply-templates mode="org" select="ecd:ConsignorDetails"/>
											</td>
											<td class="bordered">
												<xsl:if test="$rowspan_p &gt; 1">
													<xsl:attribute name="rowspan"><xsl:value-of select="$rowspan_p"/></xsl:attribute>
												</xsl:if>
												<xsl:apply-templates mode="org" select="ecd:ConsigneeDetails"/>
											</td>
											<xsl:if test="not(ecd:GoodsItemDetails)">
												<td class="bordered"/>
												<td class="bordered"/>
												<td class="bordered"/>
												<td class="bordered"/>
												<td class="bordered"/>
												<td class="bordered"/>
												<td class="bordered"/>
												<td class="bordered"/>
												<td class="bordered"/>
												<td class="bordered"/>
											</xsl:if>
											<xsl:apply-templates select="ecd:GoodsItemDetails[1]" mode="ptd"/>
										</tr>
										<xsl:for-each select="ecd:GoodsItemDetails[position() = 1]">
											<xsl:for-each select="ecd:PresentedDocDetails[position() &gt; 1]">
												<tr align="center" valign="top">
													<xsl:apply-templates select="."/>
												</tr>
											</xsl:for-each>
										</xsl:for-each>
										<xsl:for-each select="ecd:GoodsItemDetails[position() &gt; 1]">
											<tr align="center" valign="top">
												<xsl:apply-templates select="." mode="ptd"/>
											</tr>
											<xsl:for-each select="ecd:PresentedDocDetails[position() &gt; 1]">
												<tr align="center" valign="top">
													<xsl:apply-templates select="."/>
												</tr>
											</xsl:for-each>
										</xsl:for-each>
										<tr align="center" valign="top">
											<td class="bordered_fat2" colspan="8" align="left">
												Всего по индивидуальной накладной<br/>(общий вес брутто, стоимость в валюте государства-члена)
											</td>
											<td class="bordered_fat2">
												<xsl:apply-templates mode="quantity" select="ecd:UnifiedGrossWeightQuantity"/>
											</td>
											<td class="bordered_fat2"/>
											<td class="bordered_fat2">
												<xsl:apply-templates mode="pricevalue" select="ecd:CAValueAmount"/>
											</td>
											<td  class="bordered_fat2" colspan="2"/>
											<td class="bordered_fat2"  colspan="3" style="font-style:italic;font-size: 8pt;">
												<xsl:value-of select="ecd:Design"/>												
												<xsl:text> </xsl:text>
												<xsl:value-of select="ecd:TaxBase_DecisionDate"/>
											</td>
										</tr>
									</xsl:for-each>
									<tr align="center" valign="top">
										<td class="bordered_fat3" colspan="9" align="left">
											Всего по пассажирской таможенной декларации для экспресс-грузов<br/>(общий вес брутто, стоимость в валюте государства-члена)
										</td>
										<td class="bordered_fat3">
											<!-- <xsl:apply-templates mode="quantity" select="ecd:GoodsShipment/ecd:UnifiedGrossWeightQuantity"/> -->
											<xsl:value-of select="format-number(sum(//ecd:GoodsShipment/ecd:HouseShipment/ecd:UnifiedGrossWeightQuantity/cat_ru:GoodsQuantity), '###0.000')"/>
										</td>
										<td class="bordered_fat3"/>
										<td class="bordered_fat3">
											<!-- <xsl:apply-templates mode="pricevalue" select="ecd:GoodsShipment/ecd:CAValueAmount"/> -->
											<xsl:value-of select="format-number(sum(//ecd:GoodsShipment/ecd:HouseShipment/ecd:CustomsCost/ecd:CurrencyQuantity), '###0.00')"/>
										</td>
										<td class="bordered_fat3" colspan="5"/>
									</tr>
									<tr>
										<td colspan="17">
											<br/>
										</td>
									</tr>
									<tr>
										<td class="bordered" colspan="17">Сведения о лице, заполнившем КПТДЭГ, дата
											<br/>
											<br/>
											<xsl:apply-templates select="ecd:BrokerRegistryDocDetails/RUDECLcat:DocKindCode"/>
											<xsl:if test="ecd:BrokerRegistryDocDetails/RUDECLcat:RegistrationNumberId">
												<xsl:text>, </xsl:text>
												<xsl:value-of select="ecd:BrokerRegistryDocDetails/RUDECLcat:RegistrationNumberId"/>
											</xsl:if>
											<br/>
											<xsl:apply-templates select="ecd:SignatoryPerson/ecd:SigningDetails/cat_ru:PersonSurname"/>
											<xsl:text> </xsl:text>
											<xsl:apply-templates select="ecd:SignatoryPerson/ecd:SigningDetails/cat_ru:PersonName"/>
											<xsl:text> </xsl:text>
											<xsl:apply-templates select="ecd:SignatoryPerson/ecd:SigningDetails/cat_ru:PersonMiddleName"/>
											<br/>
											<xsl:apply-templates mode="identitycard" select="ecd:SignatoryPerson/ecd:SignatoryPersonIdentityDetails"/>
											<br/>
											<xsl:apply-templates select="ecd:SignatoryPerson/ecd:SigningDetails/cat_ru:PersonPost"/>
											<br/>
											<xsl:apply-templates select="ecd:SignatoryPerson/ecd:PowerOfAttorneyDetails/catESAD_cu:DocKindCode"/>
											<br/>
											<xsl:apply-templates mode="russian_date" select="ecd:SignatoryPerson/ecd:SigningDetails/RUScat_ru:SigningDate"/>
										</td>
									</tr>
									<tr>
										<td class="bordered main_title" colspan="17" valign="top">C<br/>
											<span>
												<xsl:value-of select="/ecd:ExpressCargoDeclaration/ecd:GoodsShipment[2]/ecd:HouseShipment[1]/ecd:Design[1]"/>
												<xsl:text> </xsl:text>
												<xsl:value-of select="/ecd:ExpressCargoDeclaration/ecd:GoodsShipment[2]/ecd:HouseShipment[1]/ecd:TaxBase_DecisionDate[1]"/>
												<xsl:if test="ecd:Inspector">
													<br/>
													<xsl:value-of select="ecd:Inspector"/>
													<xsl:if test="ecd:LNP">
														<xsl:text> ЛНП: </xsl:text>
														<xsl:value-of select="ecd:LNP"/>	
													</xsl:if>												
												</xsl:if>
											</span>										
										</td>
									</tr>
									<tr>
										<td class="bordered main_title" colspan="17" valign="top">D<br/>
											<span><xsl:value-of select="ecd:GoodsShipment[1]/ecd:HouseShipment[1]/ecd:BadReason"/></span>
										</td>
									</tr>
								</tbody>
							</table>
						</xsl:if>
					</xsl:if>
					<br/>
					<table cellpadding="4" width="100%">
						<tr style="font-size: 6pt;">
							<td width="1%"><sup>1</sup></td>
							<td width="99%">Декларант при вывозе товаров с таможенной территории Евразийского экономического союза.</td>
						</tr>
						<tr style="font-size: 6pt;">
							<td><sup>2</sup></td>
							<td>Декларант при ввозе товаров на таможенную территорию Евразийского экономического союза.</td>
						</tr>
					</table>
				</div>
			</body>
		</html>
	</xsl:template>

	<xsl:template mode="identitycard" match="*">
		<xsl:value-of select="RUScat_ru:CountryCode"/>
		<xsl:if test="RUScat_ru:IdentityCardName">
			<xsl:if test="RUScat_ru:CountryCode">, </xsl:if>
			<xsl:value-of select="RUScat_ru:IdentityCardName"/>
		</xsl:if>
		<xsl:if test="RUScat_ru:IdentityCardSeries or RUScat_ru:IdentityCardNumber">
			<xsl:if test="RUScat_ru:CountryCode or RUScat_ru:IdentityCardName">, </xsl:if>
			<xsl:if test="RUScat_ru:IdentityCardSeries">серия <xsl:value-of select="RUScat_ru:IdentityCardSeries"/>&#160;</xsl:if>
			<xsl:if test="RUScat_ru:IdentityCardNumber">№ <xsl:value-of select="RUScat_ru:IdentityCardNumber"/></xsl:if>
		</xsl:if>
		<xsl:if test="RUScat_ru:IdentityCardDate">
			<xsl:if test="RUScat_ru:CountryCode or RUScat_ru:IdentityCardName or RUScat_ru:IdentityCardSeries or RUScat_ru:IdentityCardNumber">, </xsl:if>
			<xsl:call-template name="russian_date">
				<xsl:with-param name="dateIn" select="RUScat_ru:IdentityCardDate"/>
			</xsl:call-template>
		</xsl:if>
	</xsl:template>
	
	<xsl:template match="*" mode="rate1">
		<xsl:variable name="rateval">
			<xsl:choose>
				<xsl:when test="RUDECLcat:Rate or RUDECLcat:Rate!=''">
					<xsl:value-of select="RUDECLcat:Rate"/>
				</xsl:when>
				<xsl:otherwise> 0 </xsl:otherwise>
			</xsl:choose>
		</xsl:variable>
		<xsl:value-of select="format-number($rateval,'0.######')"/>
		<xsl:choose>
			<xsl:when test="RUDECLcat:RateTypeCode='%'">%</xsl:when>
			<xsl:when test="RUDECLcat:RateTypeCode!='*' or not(RUDECLcat:RateTypeCode)">
				<xsl:choose>
					<xsl:when test="RUDECLcat:RateCurrencyCode">
						<xsl:text> </xsl:text>
						<xsl:apply-templates select="RUDECLcat:RateCurrencyCode"/>
						<xsl:if test="RUDECLcat:RateTNVEDQualifierCode">
							<xsl:text> за </xsl:text>
							<xsl:value-of select="RUDECLcat:WeightingFactor"/>
							<xsl:text> </xsl:text>
							<xsl:apply-templates select="RUDECLcat:RateTNVEDQualifierCode"/>
						</xsl:if>
					</xsl:when>
					<xsl:otherwise> % </xsl:otherwise>
				</xsl:choose>
			</xsl:when>
		</xsl:choose>
	</xsl:template>

	<xsl:template match="*" mode="reg_num">
		<xsl:value-of select="cat_ru:CustomsCode"/>
		<xsl:text>/</xsl:text>
		<xsl:call-template name="num_date">
			<xsl:with-param name="dateIn" select="cat_ru:RegistrationDate"/>
		</xsl:call-template>
		<xsl:text>/</xsl:text>
		<xsl:value-of select="cat_ru:GTDNumber"/>
	</xsl:template>

	<xsl:template match="ecd:GoodsItemDetails" mode="dt">
		<xsl:variable name="rowspandoc" select="count(ecd:PresentedDocDetails)"/>
		<td class="bordered" style="font-size: 9pt;">
			<xsl:if test="$rowspandoc &gt; 1">
				<xsl:attribute name="rowspan"><xsl:value-of select="$rowspandoc"/></xsl:attribute>
			</xsl:if>
			<xsl:value-of select="ecd:GoodsNumeric"/>
		</td>
		<td class="bordered" style="font-size: 9pt;word-break: break-all;">
			<xsl:if test="$rowspandoc &gt; 1">
				<xsl:attribute name="rowspan"><xsl:value-of select="$rowspandoc"/></xsl:attribute>
			</xsl:if>
			<xsl:apply-templates select="ecd:GoodsDescription"/>
		</td>
		<td class="bordered" style="font-size: 9pt;">
			<xsl:if test="$rowspandoc &gt; 1">
				<xsl:attribute name="rowspan"><xsl:value-of select="$rowspandoc"/></xsl:attribute>
			</xsl:if>
			<xsl:value-of select="ecd:GoodsTNVEDCode"/>
		</td>
		<td class="bordered" style="font-size: 9pt;">
			<xsl:if test="$rowspandoc &gt; 1">
				<xsl:attribute name="rowspan"><xsl:value-of select="$rowspandoc"/></xsl:attribute>
			</xsl:if>
			<xsl:apply-templates mode="quantityDTG" select="ecd:SupplementaryQuantity"/>
		</td>
		<td class="bordered" style="font-size: 9pt;">
			<xsl:if test="$rowspandoc &gt; 1">
				<xsl:attribute name="rowspan"><xsl:value-of select="$rowspandoc"/></xsl:attribute>
			</xsl:if>
			<xsl:apply-templates mode="quantity" select="ecd:GrossWeightQuantity"/>
		</td>
		<td class="bordered" style="font-size: 9pt;">
			<xsl:if test="$rowspandoc &gt; 1">
				<xsl:attribute name="rowspan"><xsl:value-of select="$rowspandoc"/></xsl:attribute>
			</xsl:if>
			<xsl:apply-templates mode="quantity" select="ecd:NetWeightQuantity"/>
		</td>
		<td class="bordered" style="font-size: 9pt;">
			<xsl:if test="$rowspandoc &gt; 1">
				<xsl:attribute name="rowspan"><xsl:value-of select="$rowspandoc"/></xsl:attribute>
			</xsl:if>			
			<xsl:apply-templates mode="pricevalue" select="ecd:CAValueAmount[1]"/>
		</td>
		<td class="bordered" style="font-size: 9pt;">
			<xsl:if test="$rowspandoc &gt; 1">
				<xsl:attribute name="rowspan"><xsl:value-of select="$rowspandoc"/></xsl:attribute>
			</xsl:if>			
			<xsl:apply-templates mode="pricevalue" select="ecd:CustomsCost"/>
		</td>
		<xsl:if test="not(ecd:PresentedDocDetails)">
			<td class="bordered"/>
			<td class="bordered"/>
		</xsl:if>
		<xsl:apply-templates select="ecd:PresentedDocDetails[1]"/>
		<td class="bordered" align="left" style="font-size: 9pt;">
			<xsl:if test="$rowspandoc &gt; 1">
				<xsl:attribute name="rowspan"><xsl:value-of select="$rowspandoc"/></xsl:attribute>
			</xsl:if>
			<xsl:apply-templates mode="changeCode" select="parent::node()/ecd:ChangeDetails"/>
			<br/>
			<xsl:apply-templates mode="changeCode" select="ecd:ChangeDetails"/>		
			<br/>
			<xsl:apply-templates select="ecd:Note"/>
		</td>
	</xsl:template>
	
	<xsl:template match="ecd:GoodsItemDetails" mode="dt2">
		<xsl:variable name="rowspandoc" select="count(ecd:PresentedDocDetails)"/>
		<td class="bordered" style="font-size: 9pt;">
			<xsl:if test="$rowspandoc &gt; 1">
				<xsl:attribute name="rowspan"><xsl:value-of select="$rowspandoc"/></xsl:attribute>
			</xsl:if>
			<xsl:value-of select="ecd:GoodsNumeric"/>
		</td>
		<td class="bordered" style="font-size: 9pt;word-break: break-all;">
			<xsl:if test="$rowspandoc &gt; 1">
				<xsl:attribute name="rowspan"><xsl:value-of select="$rowspandoc"/></xsl:attribute>
			</xsl:if>
			<xsl:apply-templates select="ecd:GoodsDescription"/>
		</td>
		<td class="bordered" style="font-size: 9pt;">
			<xsl:if test="$rowspandoc &gt; 1">
				<xsl:attribute name="rowspan"><xsl:value-of select="$rowspandoc"/></xsl:attribute>
			</xsl:if>
			<xsl:value-of select="ecd:GoodsTNVEDCode"/>
		</td>
		<td class="bordered" style="font-size: 9pt;">
			<xsl:if test="$rowspandoc &gt; 1">
				<xsl:attribute name="rowspan"><xsl:value-of select="$rowspandoc"/></xsl:attribute>
			</xsl:if>
			<xsl:apply-templates mode="quantityDTG" select="ecd:SupplementaryQuantity"/>
		</td>
		<td class="bordered" style="font-size: 9pt;">
			<xsl:if test="$rowspandoc &gt; 1">
				<xsl:attribute name="rowspan"><xsl:value-of select="$rowspandoc"/></xsl:attribute>
			</xsl:if>
			<xsl:apply-templates mode="quantity" select="ecd:GrossWeightQuantity"/>
		</td>
		<td class="bordered" style="font-size: 9pt;">
			<xsl:if test="$rowspandoc &gt; 1">
				<xsl:attribute name="rowspan"><xsl:value-of select="$rowspandoc"/></xsl:attribute>
			</xsl:if>
			<xsl:apply-templates mode="quantity" select="ecd:NetWeightQuantity"/>
		</td>
		<td class="bordered" style="font-size: 9pt;">
			<xsl:if test="$rowspandoc &gt; 1">
				<xsl:attribute name="rowspan"><xsl:value-of select="$rowspandoc"/></xsl:attribute>
			</xsl:if>			
			<xsl:apply-templates mode="pricevalue" select="ecd:CAValueAmount[1]"/>
		</td>
		<td class="bordered" style="font-size: 9pt;">
			<xsl:if test="$rowspandoc &gt; 1">
				<xsl:attribute name="rowspan"><xsl:value-of select="$rowspandoc"/></xsl:attribute>
			</xsl:if>			
			<xsl:apply-templates mode="pricevalue" select="ecd:CAValueAmount[2]"/>
		</td>
		<xsl:if test="not(ecd:PresentedDocDetails)">
			<td class="bordered"/>
			<td class="bordered"/>
		</xsl:if>
		<xsl:apply-templates select="ecd:PresentedDocDetails[1]"/>
		<td class="bordered" align="left" style="font-size: 9pt;">
			<xsl:if test="$rowspandoc &gt; 1">
				<xsl:attribute name="rowspan"><xsl:value-of select="$rowspandoc"/></xsl:attribute>
			</xsl:if>
			<xsl:apply-templates mode="changeCode" select="parent::node()/ecd:ChangeDetails"/>
			<br/>
			<xsl:apply-templates mode="changeCode" select="ecd:ChangeDetails"/>		
			<br/>
			<xsl:apply-templates select="ecd:Note"/>
		</td>
	</xsl:template>

	<xsl:template match="ecd:GoodsItemDetails" mode="ptd">
		<xsl:variable name="rowspandoc" select="count(ecd:PresentedDocDetails)"/>
		<td class="bordered" style="font-size: 9pt;">
			<xsl:if test="$rowspandoc &gt; 1">
				<xsl:attribute name="rowspan"><xsl:value-of select="$rowspandoc"/></xsl:attribute>
			</xsl:if>
			<xsl:value-of select="ecd:GoodsNumeric"/>
			<xsl:text>/</xsl:text>
			<xsl:value-of select="ecd:TotalGoodsNumeric"/>		
		</td>
		<td class="bordered" style="font-size: 9pt;word-break: break-all;">
			<xsl:if test="$rowspandoc &gt; 1">
				<xsl:attribute name="rowspan"><xsl:value-of select="$rowspandoc"/></xsl:attribute>
			</xsl:if>
			<xsl:apply-templates select="ecd:GoodsDescription"/>
		</td>
		<td class="bordered" style="font-size: 9pt;">
			<xsl:if test="$rowspandoc &gt; 1">
				<xsl:attribute name="rowspan"><xsl:value-of select="$rowspandoc"/></xsl:attribute>
			</xsl:if>
			<xsl:value-of select="ecd:GoodsTNVEDCode"/>
		</td>
		<td class="bordered" style="font-size: 9pt;">
			<xsl:if test="$rowspandoc &gt; 1">
				<xsl:attribute name="rowspan"><xsl:value-of select="$rowspandoc"/></xsl:attribute>
			</xsl:if>
			<xsl:apply-templates mode="quantity" select="ecd:SupplementaryQuantity"/>
		</td>
		<td class="bordered" style="font-size: 9pt;">
			<xsl:if test="$rowspandoc &gt; 1">
				<xsl:attribute name="rowspan"><xsl:value-of select="$rowspandoc"/></xsl:attribute>
			</xsl:if>
			<xsl:apply-templates mode="quantity" select="ecd:GrossWeightQuantity"/>
		</td>
		<td class="bordered" style="font-size: 9pt;">
			<xsl:if test="$rowspandoc &gt; 1">
				<xsl:attribute name="rowspan"><xsl:value-of select="$rowspandoc"/></xsl:attribute>
			</xsl:if>
			<xsl:apply-templates mode="pricevalue" select="ecd:CAValueAmount[1]"/>
		</td>
		<td class="bordered" style="font-size: 9pt;">
			<xsl:if test="$rowspandoc &gt; 1">
				<xsl:attribute name="rowspan"><xsl:value-of select="$rowspandoc"/></xsl:attribute>
			</xsl:if>			
				<!--<xsl:apply-templates mode="pricevalue" select="ecd:CAValueAmount[2]"/>-->
				<xsl:apply-templates mode="pricevalue" select="ecd:CustomsCost"/>
		</td>
		<xsl:if test="not(ecd:PresentedDocDetails)">
			<td class="bordered"/>
			<td class="bordered"/>			
		</xsl:if>
		<xsl:apply-templates select="ecd:PresentedDocDetails[1]"/>
		<td class="bordered" colspan="3" align="left" style="font-size: 9pt;">
			<xsl:if test="$rowspandoc &gt; 1">
				<xsl:attribute name="rowspan"><xsl:value-of select="$rowspandoc"/></xsl:attribute>
			</xsl:if>
			<xsl:apply-templates mode="changeCode" select="parent::node()/ecd:ChangeDetails"/>
			<br/>
			<xsl:apply-templates mode="changeCode" select="ecd:ChangeDetails"/>		
			<br/>
			<xsl:apply-templates select="ecd:Note"/>
		</td>
	</xsl:template>
	<xsl:template match="*" mode="changeCode">			
		<xsl:value-of select="ecd:StageChangeCode"/>
		<xsl:text>/</xsl:text>
		<xsl:value-of select="ecd:ReasonChangeCode"/>
		<xsl:text>/</xsl:text>
		<xsl:value-of select="ecd:QuantityChangeCode"/>
		<xsl:text>/</xsl:text>
		<xsl:value-of select="ecd:TNVEDChangeCode"/>
		<xsl:text>/</xsl:text>
		<xsl:value-of select="ecd:CustomsCostChangeCode"/>
		<xsl:text>/</xsl:text>
		<xsl:value-of select="ecd:CustomsPaymentChangeCode"/>
		<xsl:text>/</xsl:text>
		<xsl:value-of select="ecd:OtherChangeCode"/>		
	</xsl:template>
	<xsl:template match="ecd:PresentedDocDetails">
		
				<td class="bordered" style="font-size: 9pt;">			
					<xsl:if test="RUDECLcat:DocKindCode">
						<xsl:value-of select="RUDECLcat:DocKindCode"/>
						<xsl:if test="ecd:DocumentPresentingDetails/RUDECLcat:DocPresentKindCode">
							<xsl:text> / </xsl:text>
							<xsl:value-of select="ecd:DocumentPresentingDetails/RUDECLcat:DocPresentKindCode"/>
						</xsl:if>
					</xsl:if>									
				</td>
				<td class="bordered" style="font-size: 9pt;">
					<xsl:choose>
						<xsl:when test="cat_ru:PrDocumentNumber">
							<xsl:call-template name="russian_date">
								<xsl:with-param name="dateIn" select="cat_ru:PrDocumentDate"/>						
							</xsl:call-template>
							<xsl:if test="cat_ru:PrDocumentDate">
								<xsl:text> / </xsl:text>
							</xsl:if>							
							<xsl:value-of select="cat_ru:PrDocumentNumber"/>		
						</xsl:when>
						<xsl:otherwise>
							<xsl:text>-/-</xsl:text>	
						</xsl:otherwise>
					</xsl:choose>
				</td>	
			
		
	</xsl:template>

	<xsl:template match="*" mode="pricevalue">
		<xsl:if test="ecd:CurrencyQuantity">
			<xsl:value-of select="ecd:CurrencyCode"/>
			<xsl:text> / </xsl:text>
			<xsl:value-of select="ecd:CurrencyQuantity"/>
		</xsl:if>		
	</xsl:template>	
	
	<xsl:template match="*" mode="quantityDTG">
		<xsl:if test="cat_ru:MeasureUnitQualifierName">			
			<xsl:value-of select="cat_ru:MeasureUnitQualifierName"/>
			<xsl:text> / </xsl:text>
		</xsl:if>
		<xsl:if test="cat_ru:MeasureUnitQualifierCode">
			<xsl:text>(</xsl:text>			
			<xsl:value-of select="cat_ru:MeasureUnitQualifierCode"/>
			<xsl:text>) / </xsl:text>
		</xsl:if>
		<xsl:value-of select="cat_ru:GoodsQuantity"/>				
	</xsl:template>
	
	<xsl:template match="*" mode="quantity">
		<xsl:value-of select="cat_ru:GoodsQuantity"/>
		<xsl:if test="cat_ru:MeasureUnitQualifierName">
			<xsl:text> </xsl:text>
			<xsl:value-of select="cat_ru:MeasureUnitQualifierName"/>
		</xsl:if>
		<!--<xsl:if test="cat_ru:MeasureUnitQualifierCode">
			<xsl:text> (</xsl:text>
			<xsl:value-of select="cat_ru:MeasureUnitQualifierCode"/>
			<xsl:text>)</xsl:text>
		</xsl:if>-->
	</xsl:template>
	<xsl:template match="*" mode="org">
		<xsl:value-of select="cat_ru:OrganizationName"/>
		<xsl:if test="cat_ru:ShortName and not(cat_ru:OrganizationName)">
			<xsl:value-of select="cat_ru:ShortName"/>
		</xsl:if>
		<xsl:text> </xsl:text>
		<xsl:choose>
			<xsl:when test="cat_ru:RFOrganizationFeatures">				
					<xsl:apply-templates select="cat_ru:RFOrganizationFeatures"/>				
			</xsl:when>
			<xsl:otherwise>
				<xsl:text></xsl:text>
					<xsl:apply-templates select="RUScat_ru:SubjectAddressDetails"/>
			</xsl:otherwise>
		</xsl:choose>	
		
		<xsl:apply-templates select="RUScat_ru:CommunicationDetails"/>
	</xsl:template>
	
	<xsl:template name="RUScat_ru:SubjectAddressDetails" match="RUScat_ru:SubjectAddressDetails">
		<br/>
		<xsl:if test="RUScat_ru:PostalCode">
			<span class="normal">
				<xsl:value-of select="RUScat_ru:PostalCode"/>
			</span>
			<span class="normal">,</span>
		</xsl:if>
		<xsl:if test="RUScat_ru:CountryCode">
			<span class="normal"> </span>
			<span class="normal">
				<xsl:value-of select="RUScat_ru:CountryCode"/>
			</span>			
		</xsl:if>
		<xsl:if test="RUScat_ru:CounryName">
			<span class="normal">,</span>
			<span class="normal">
				<xsl:value-of select="RUScat_ru:CounryName"/>
			</span>			
		</xsl:if>
		<xsl:if test="RUScat_ru:Region">
			<span class="normal">,</span>
			<span class="normal">
				<xsl:value-of select="RUScat_ru:Region"/>
			</span>			
		</xsl:if>
		<xsl:if test="RUScat_ru:District">
			<span class="normal">,</span>
			<span class="normal">
				<xsl:value-of select="RUScat_ru:District"/>
			</span>			
		</xsl:if>
		<xsl:if test="RUScat_ru:Town">
			<span class="normal">,</span>
			<span class="normal">
				<xsl:value-of select="RUScat_ru:Town"/>
			</span>			
		</xsl:if>
		<xsl:if test="RUScat_ru:City">
			<span class="normal">,</span>
			<span class="normal">
				<xsl:value-of select="RUScat_ru:City"/>
			</span>
		</xsl:if>
		<xsl:if test="RUScat_ru:StreetHouse">
			<span class="normal">,</span>
			<span class="normal">
				<xsl:value-of select="RUScat_ru:StreetHouse"/>
			</span>			
		</xsl:if>
		<xsl:if test="RUScat_ru:House">
			<span class="normal">,</span>
			<span class="normal">
				<xsl:value-of select="RUScat_ru:House"/>
			</span>			
		</xsl:if>
		<xsl:if test="RUScat_ru:Room">
			<span class="normal">
				<xsl:value-of select="RUScat_ru:Room"/>
			</span>			
		</xsl:if>
		<xsl:if test="RUScat_ru:AddressText">
			<span class="normal">,</span>
			<span class="normal">
				<xsl:value-of select="RUScat_ru:AddressText"/>
			</span>			
		</xsl:if>		
		<xsl:if test="RUScat_ru:PostOfficeBoxId">
			<span class="normal">, </span>
			<span class="normal">
				<xsl:value-of select="RUScat_ru:PostOfficeBoxId"/>
			</span>
			<span class="normal">,</span>
		</xsl:if>
	</xsl:template>
	
	<xsl:template match="RUScat_ru:CommunicationDetails">
		<xsl:if test="cat_ru:Phone">
			<br/>
			<xsl:text> т.: </xsl:text>
			<xsl:for-each select="cat_ru:Phone">
				<xsl:if test="position()!=1">, </xsl:if>
				<xsl:value-of select="."/>
			</xsl:for-each>
		</xsl:if>
		<xsl:if test="cat_ru:Fax">
			<xsl:text> факс: </xsl:text>
			<xsl:value-of select="cat_ru:Fax"/>
		</xsl:if>
		<xsl:if test="cat_ru:Telex">
			<xsl:text> телекс: </xsl:text>
			<xsl:value-of select="cat_ru:Telex"/>
		</xsl:if>
		<xsl:if test="cat_ru:E_mail">
			<xsl:text> e-mail: </xsl:text>
			<xsl:for-each select="cat_ru:E_mail">
				<xsl:if test="position()!=1">, </xsl:if>
				<xsl:value-of select="."/>
			</xsl:for-each>
		</xsl:if>
	</xsl:template>
	<xsl:template match="cat_ru:RFOrganizationFeatures">
		<br/>		
		<xsl:if test="cat_ru:INN">
			<xsl:text> </xsl:text>
			<xsl:value-of select="cat_ru:INN"/>			
		</xsl:if>
		<xsl:if test="cat_ru:KPP">
			<xsl:text> / </xsl:text>
			<xsl:value-of select="cat_ru:KPP"/>
		</xsl:if>
	</xsl:template>
	<xsl:template match="*" mode="doc">
		<xsl:if test="cat_ru:PrDocumentNumber">
			<xsl:value-of select="cat_ru:PrDocumentNumber"/>
		</xsl:if>
	</xsl:template>
	<xsl:template name="num_date">
		<xsl:param name="dateIn"/>
		<xsl:choose>
			<xsl:when test="substring($dateIn,5,1)='-' and substring($dateIn,8,1)='-'">
				<xsl:value-of select="substring($dateIn,9,2)"/>
				<xsl:text/>
				<xsl:value-of select="substring($dateIn,6,2)"/>
				<xsl:text/>
				<xsl:value-of select="substring($dateIn,3,2)"/>
			</xsl:when>
			<xsl:otherwise>
				<xsl:value-of select="$dateIn"/>
			</xsl:otherwise>
		</xsl:choose>
	</xsl:template>
	<xsl:template name="russian_date">
		<xsl:param name="dateIn"/>
		<xsl:choose>
			<xsl:when test="substring($dateIn,5,1)='-' and substring($dateIn,8,1)='-'">
				<xsl:value-of select="substring($dateIn,9,2)"/>
				<xsl:text>.</xsl:text>
				<xsl:value-of select="substring($dateIn,6,2)"/>
				<xsl:text>.</xsl:text>
				<xsl:value-of select="substring($dateIn,1,4)"/>
			</xsl:when>
			<xsl:otherwise>
				<xsl:value-of select="$dateIn"/>
			</xsl:otherwise>
		</xsl:choose>
	</xsl:template>
	<xsl:variable name="cmt" select="//comment()"/>
	<xsl:variable name="num" select="normalize-space(substring-before($cmt, ' :'))"/>
	<xsl:variable name="api_qr">        
		<xsl:if test="string-length($num)!=0">
			<xsl:value-of select="concat('https://www.alta.ru/tools/qrcode/?text=',$num)"/>
		</xsl:if>        
	</xsl:variable>
	<xsl:template name="comment_dt">
		<xsl:apply-templates select="//comment()"/>
	</xsl:template>
	<xsl:template match="comment()">
		<xsl:if test="position() = 1">
			<xsl:value-of select="."/>
		</xsl:if>
	</xsl:template>
	<xsl:template name="comment_dt_C">
		<xsl:if test="position() = 1">
			<xsl:variable name="cmt0" select="//comment()"/>
			<xsl:variable name="cmt" select="substring-after($cmt0, ':')"/>
			<xsl:value-of select="substring-before($cmt, '~')"/>
			<span class="bold">
				<br/>
				<xsl:text>ФИО Должностного лица ТО:</xsl:text>&#160;
				<xsl:value-of select="substring-after($cmt, '~')"/>
			</span>
		</xsl:if>
	</xsl:template>
	
	<xsl:template name="gtd_date">
		<xsl:param name="dateIn"/>
		<xsl:choose>
			<xsl:when test="substring($dateIn,5,1)='-' and substring($dateIn,8,1)='-'">
				<xsl:value-of select="substring($dateIn,9,2)"/>
				<xsl:value-of select="substring($dateIn,6,2)"/>
				<xsl:value-of select="substring($dateIn,3,2)"/>
			</xsl:when>
			<xsl:otherwise>
				<xsl:value-of select="$dateIn"/>
			</xsl:otherwise>
		</xsl:choose>
	</xsl:template>	
	
	<xsl:template name="PrecedingDocDetails_info">
		<xsl:copy>
			<xsl:variable name="precedingDoc" select="ecd:GoodsShipment/ecd:HouseShipment/ecd:GoodsItemDetails/ecd:PrecedingDocDetails"/>			
			<xsl:if test="$precedingDoc">
				<xsl:if test="$precedingDoc/ecd:PrecedingDocumentModeCode">
					<xsl:value-of select="concat($precedingDoc/ecd:PrecedingDocumentModeCode,'-')"/>					
					<xsl:if test="$precedingDoc/ecd:CustomsDocIdDetails/cat_ru:CustomsCode">
						<xsl:value-of select="$precedingDoc/ecd:CustomsDocIdDetails/cat_ru:CustomsCode"/>						
						<xsl:if test="$precedingDoc/ecd:CustomsDocIdDetails/cat_ru:RegistrationDate">
							<xsl:text>/</xsl:text>
							<xsl:call-template name="gtd_date">
								<xsl:with-param name="dateIn" select="$precedingDoc/ecd:CustomsDocIdDetails/cat_ru:RegistrationDate"/>
							</xsl:call-template>
						</xsl:if>						
						<xsl:if test="$precedingDoc/ecd:CustomsDocIdDetails/cat_ru:GTDNumber">
							<xsl:text>/</xsl:text>
							<xsl:value-of select="$precedingDoc/ecd:CustomsDocIdDetails/cat_ru:GTDNumber"/>
						</xsl:if>
						<xsl:if test="$precedingDoc/ecd:CustomsDocIdDetails/RUDECLcat:Code">
							<xsl:text>/</xsl:text>
							<xsl:value-of select="$precedingDoc/ecd:CustomsDocIdDetails/RUDECLcat:Code"/>
						</xsl:if>
					</xsl:if>
					<xsl:if test="$precedingDoc/ecd:PIDocumentNumber">
						<xsl:value-of select="$precedingDoc/ecd:PIDocumentNumber/ecd:CountryCode"/>						
						<xsl:if test="$precedingDoc/ecd:PIDocumentNumber/ecd:PIDate">
							<xsl:text>/</xsl:text>
							<xsl:call-template name="gtd_date">
								<xsl:with-param name="dateIn" select="$precedingDoc/ecd:PIDocumentNumber/ecd:PIDate"/>
							</xsl:call-template>
						</xsl:if>						
						<xsl:if test="$precedingDoc/ecd:PIDocumentNumber/ecd:PrecedingDocumentModeCode">
							<xsl:text>/</xsl:text>
							<xsl:value-of select="$precedingDoc/ecd:PIDocumentNumber/ecd:PrecedingDocumentModeCode"/>
						</xsl:if>
					</xsl:if>
					<xsl:if test="$precedingDoc/ecd:TIRIdDetails">						
						<xsl:value-of select="$precedingDoc/ecd:TIRIdDetails/catESAD_cu:TIRSeries"/>
						<xsl:value-of select="$precedingDoc/ecd:TIRIdDetails/catESAD_cu:TIRID"/>
					</xsl:if>					
				</xsl:if>
			</xsl:if>			
			
		</xsl:copy>
	</xsl:template>	
	
	<xsl:template match="ecd:PowerOfAttorneyDetails">
		<xsl:apply-templates select="cat_ru:PrDocumentName"/>
		<xsl:if test="cat_ru:PrDocumentNumber">
			<xsl:text> № </xsl:text>
			<xsl:apply-templates select="cat_ru:PrDocumentNumber"/>
		</xsl:if>
		<xsl:if test="cat_ru:PrDocumentDate">
			<xsl:text> от </xsl:text>
			<xsl:apply-templates mode="russian_date" select="cat_ru:PrDocumentDate"/>
		</xsl:if>
		<xsl:if test="catESAD_cu:DocStartDate">
			<xsl:text>, начало </xsl:text>
			<xsl:apply-templates mode="russian_date" select="catESAD_cu:DocStartDate"/>
		</xsl:if>
		<xsl:if test="catESAD_cu:DocValidityDate">
			<xsl:text>, окончание </xsl:text>
			<xsl:apply-templates mode="russian_date" select="catESAD_cu:DocValidityDate"/>
		</xsl:if>
		<xsl:if test="catESAD_cu:CountryCode">
			<xsl:text>, код страны </xsl:text>
			<xsl:apply-templates select="catESAD_cu:CountryCode"/>
		</xsl:if>
		<xsl:if test="catESAD_cu:DocKindCode">
			<xsl:text>, код вида </xsl:text>
			<xsl:apply-templates select="catESAD_cu:DocKindCode"/>
		</xsl:if>
	</xsl:template>
	<xsl:template match="ecd:ChangeDetails">
		<xsl:text>Этап: </xsl:text><xsl:value-of select="ecd:StageChangeCode"/><br/>
		<xsl:text>Обстоятельства: </xsl:text><xsl:value-of select="ecd:ReasonChangeCode"/><br/>
		<xsl:text>Количество: </xsl:text><xsl:value-of select="ecd:QuantityChangeCode"/><br/>
		<xsl:text>Код по ТН ВЭД: </xsl:text><xsl:value-of select="ecd:TNVEDChangeCode"/><br/>
		<xsl:text>Стомость: </xsl:text><xsl:value-of select="ecd:CustomsCostChangeCode"/><br/>
		<xsl:text>Исчисления: </xsl:text><xsl:value-of select="ecd:CustomsPaymentChangeCode"/><br/>
		<xsl:text>Иные сведения: </xsl:text><xsl:value-of select="ecd:OtherChangeCode"/>
	</xsl:template>
	
	<xsl:template match="ecd:PrecedingDocDetails">
		<xsl:if test="position()!=1">, </xsl:if>
		<xsl:for-each select="*">
			<xsl:if test="position()!=1">
				<xsl:text>-</xsl:text>
			</xsl:if>
			<xsl:choose>
				<xsl:when test="contains(local-name(), 'Date')">
					<xsl:apply-templates mode="russian_date" select="."/>
				</xsl:when>
				<xsl:otherwise>
					<xsl:apply-templates select="."/>
				</xsl:otherwise>
			</xsl:choose>
		</xsl:for-each>
	</xsl:template>
	
	<xsl:template match="ecd:CustomsDocIdDetails|ecd:PIDocumentNumber">
		<xsl:apply-templates mode="reg_num" select="."/>
	</xsl:template>
	<xsl:template match="ecd:TIRIdDetails">		
		<xsl:for-each select="*">
			<xsl:if test="position()!=1">
				<xsl:text>/</xsl:text>
			</xsl:if>
			<xsl:apply-templates select="."/>
		</xsl:for-each>
	</xsl:template>
	<xsl:template name="get_xpath">
		<xsl:param name="node" select="."/>
		<xsl:variable name="xpath">
			<xsl:for-each select="$node/ancestor-or-self::*">
				<xsl:variable name="name">
					<xsl:value-of select="name()"/>
				</xsl:variable>
				<xsl:variable name="pos">
					<xsl:value-of select="count(node()/parent::*/preceding-sibling::*[name()=$name])"/>
				</xsl:variable>
				<xsl:choose>
					<xsl:when test="position()=last()">
						<xsl:value-of select="concat($name,'[',$pos,']')"/>
					</xsl:when>
					<xsl:otherwise>
						<xsl:value-of select="concat($name,'[',$pos,']','/')"/>
					</xsl:otherwise>
				</xsl:choose>
			</xsl:for-each>
		</xsl:variable>
		<xsl:value-of select="$xpath"/>
	</xsl:template>
	<xsl:template match="//*[local-name()='ExpressCargoDeclaration']//*" priority="-1">
		<xsl:variable name="xpath">
			<xsl:call-template name="get_xpath">
				<xsl:with-param name="node" select="current()"/>
			</xsl:call-template>
		</xsl:variable>
		<element xml_node="{$xpath}">
			<xsl:apply-templates/>
		</element>
	</xsl:template>
	<xsl:template match="//*[local-name()='ExpressCargoDeclarationCustomMark']//*" priority="-1">
		<xsl:variable name="xpath">
			<xsl:call-template name="get_xpath">
				<xsl:with-param name="node" select="current()"/>
			</xsl:call-template>
		</xsl:variable>
		<element xml_node="{$xpath}">
			<xsl:apply-templates/>
		</element>
	</xsl:template>
	<xsl:template match="*" mode="translate_number">
		<xsl:variable name="xpath_date">
			<xsl:call-template name="get_xpath">
				<xsl:with-param name="node" select="."/>
			</xsl:call-template>
		</xsl:variable>
		<element xml_node="{$xpath_date}">
			<xsl:value-of select="translate(.,'.', ',')"/>
		</element>
	</xsl:template>
</xsl:stylesheet>
